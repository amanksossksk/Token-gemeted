import os
import json
import time
import random
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, jsonify

import undetected_chromedriver as uc
from undetected_chromedriver import patcher
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# ---------- termux patch ----------
_orig_init = patcher.Patcher.__init__
def _patched_init(self, *args, **kwargs):
    _orig_init(self, *args, **kwargs)
    if self.executable_path and self.executable_path.endswith('.exe'):
        self.executable_path = self.executable_path[:-4]
patcher.Patcher.__init__ = _patched_init

# ---------- config ----------
TOKEN_FILE        = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tokens.json")
TOKEN_TTL_SEC     = 20 * 60
MIN_POOL          = 30          # keep at least this many ready
MAX_POOL          = 250        # hard ceiling on stored tokens
TARGET_POOL       = 50         # refiller keeps topping up to here
WORKERS           = 2          # start conservative for termux
MAX_WORKERS       = 2          # never exceed this on mobile
REFILL_INTERVAL   = 30
HARVEST_TIMEOUT   = 120

_pool_lock      = threading.Lock()
_harvest_sem    = threading.Semaphore(WORKERS)   # caps concurrent chromes
_stop_flag      = threading.Event()
_stats_lock     = threading.Lock()
_stats = {"harvested": 0, "failed": 0, "purged": 0, "running": 0}

# ---------- token store (same as before) ----------
def _now(): return int(time.time())

def _load_pool():
    if not os.path.exists(TOKEN_FILE): return []
    try:
        with open(TOKEN_FILE) as f: data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []

def _save_pool(pool):
    tmp = TOKEN_FILE + ".tmp"
    with open(tmp, "w") as f: json.dump(pool, f)
    os.replace(tmp, TOKEN_FILE)

def _purge_stale(pool):
    cutoff = _now() - TOKEN_TTL_SEC
    fresh = [t for t in pool if t.get("created_at", 0) >= cutoff]
    removed = len(pool) - len(fresh)
    if removed:
        with _stats_lock: _stats["purged"] += removed
    return fresh

def _pool_size():
    with _pool_lock:
        pool = _purge_stale(_load_pool())
        _save_pool(pool)
        return len(pool)

def _pop_fresh_token():
    with _pool_lock:
        pool = _purge_stale(_load_pool())
        token = pool.pop(0) if pool else None
        if token: token["last_used"] = _now()
        _save_pool(pool)
        return token, len(pool)

def _push_token(entry):
    with _pool_lock:
        pool = _purge_stale(_load_pool())
        if len(pool) >= MAX_POOL: return False
        pool.append(entry)
        _save_pool(pool)
        return True

# ---------- driver / harvest ----------
def make_driver():
    opts = uc.ChromeOptions()
    opts.binary_location = "/data/data/com.termux/files/usr/bin/chromium-browser"
    for a in (
        "--headless=new", "--no-sandbox", "--disable-dev-shm-usage",
        "--ignore-certificate-errors", "--disable-blink-features=AutomationControlled",
        "--disable-session-crashed-bubble", "--disable-gpu",
        "--disable-features=IsolateOrigins,site-per-process",
        "--disable-background-networking", "--disable-extensions",
        "--no-first-run", "--no-default-browser-check",
        "--window-size=412,915",
    ):
        opts.add_argument(a)
    return uc.Chrome(
        options=opts,
        driver_executable_path="/data/data/com.termux/files/usr/bin/chromedriver",
        browser_executable_path="/data/data/com.termux/files/usr/bin/chromium-browser",
        version_main=140,
        use_subprocess=True,
        patcher_force_close=True,
    )

def _install_hook(driver):
    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": """
        (function(){
            window.__vqd_captures = [];
            const of = window.fetch;
            window.fetch = async function(){
                const a = arguments;
                const url = (typeof a[0]==='string') ? a[0] : a[0].url;
                if (url && url.includes('duckchat/v1/chat')) {
                    const h = (a[1] && a[1].headers) || {};
                    window.__vqd_captures.push({
                        vqd: h['x-vqd-hash-1'] || h['X-Vqd-Hash-1'] || null,
                        fe:  h['x-fe-signals'] || null,
                    });
                }
                return of.apply(this, a);
            };
            const oo = XMLHttpRequest.prototype.open;
            XMLHttpRequest.prototype.open = function(m,u){ this.__u=u; return oo.apply(this,arguments); };
            const os_ = XMLHttpRequest.prototype.setRequestHeader;
            XMLHttpRequest.prototype.setRequestHeader = function(n,v){
                if (this.__u && this.__u.includes('duckchat/v1/chat') &&
                    n.toLowerCase()==='x-vqd-hash-1')
                    window.__vqd_captures.push({vqd:v});
                return os_.apply(this,arguments);
            };
        })();
    """})

def dismiss_consent(driver, timeout=8):
    from selenium.common.exceptions import TimeoutException, NoSuchElementException
    try:
        btn = WebDriverWait(driver, timeout).until(
            EC.element_to_be_clickable((By.XPATH, "//button[contains(., 'Continue')]"))
        )
        btn.click()
        time.sleep(1.0)
        return True
    except (TimeoutException, NoSuchElementException):
        return False

def harvest_one(prompt_text="Hi"):
    """Acquire semaphore, run one harvest, release. Returns entry or None."""
    if not _harvest_sem.acquire(timeout=HARVEST_TIMEOUT):
        return None
    with _stats_lock: _stats["running"] += 1
    driver = None
    try:
        driver = make_driver()
        _install_hook(driver)
        driver.get("https://duck.ai/")
        box = WebDriverWait(driver, 40).until(
            EC.presence_of_element_located((By.TAG_NAME, "textarea"))
        )
        box.send_keys(prompt_text)
        time.sleep(random.uniform(0.8, 1.6))
        box.send_keys("\n")
        time.sleep(random.uniform(4.0, 6.0))
        dismiss_consent(driver, timeout=8)
        captures = driver.execute_script("return window.__vqd_captures || [];")
        for cap in captures:
            if cap.get("vqd"):
                with _stats_lock: _stats["harvested"] += 1
                return {
                    "vqd": cap["vqd"],
                    "fe":  cap.get("fe"),
                    "created_at": _now(),
                    "last_used": None,
                }
        with _stats_lock: _stats["failed"] += 1
        return None
    except Exception:
        with _stats_lock: _stats["failed"] += 1
        traceback.print_exc()
        return None
    finally:
        if driver:
            try: driver.quit()
            except Exception: pass
        with _stats_lock: _stats["running"] -= 1
        _harvest_sem.release()

# ---------- bulk harvest with worker pool ----------
def bulk_harvest(count):
    """Kick off `count` harvests across the worker pool. Non-blocking."""
    pool = ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="harvest")

    def _job():
        if _stop_flag.is_set(): return
        entry = harvest_one()
        if entry:
            _push_token(entry)

    futures = [pool.submit(_job) for _ in range(count)]
    return pool, futures

# ---------- background refiller (single scheduler + worker pool) ----------
def refiller_loop():
    """Keeps pool topped up to TARGET_POOL. Uses worker pool for parallelism."""
    executor = ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="refill")
    while not _stop_flag.is_set():
        try:
            size = _pool_size()
            if size < MIN_POOL:
                deficit = TARGET_POOL - size
                # submit enough jobs to reach target
                for _ in range(min(deficit, MAX_WORKERS * 2)):
                    executor.submit(_one_harvest_job)
            elif size < TARGET_POOL:
                # gentle top-up — one at a time
                executor.submit(_one_harvest_job)
        except Exception:
            traceback.print_exc()
        _stop_flag.wait(timeout=REFILL_INTERVAL)

def _one_harvest_job():
    if _pool_size() >= MAX_POOL: return
    entry = harvest_one()
    if entry:
        _push_token(entry)
        print(f"[refill] +1, pool={_pool_size()}")

def start_refiller():
    t = threading.Thread(target=refiller_loop, daemon=True, name="vqd-scheduler")
    t.start()
    return t

# ---------- flask ----------
app = Flask(__name__)

@app.route("/token", methods=["GET"])
def get_token():
    token, pool_left = _pop_fresh_token()
    if token is None:
        return jsonify({
            "success": False,
            "error": "no token — bulk-harvest to warm the pool",
            "hint":   "POST /harvest?count=100",
        }), 503
    return jsonify({
        "success": True,
        "vqd": token["vqd"],
        "fe":  token.get("fe"),
        "created_at": token["created_at"],
        "expires_at": token["created_at"] + TOKEN_TTL_SEC,
        "pool_remaining": pool_left,
    })

@app.route("/harvest", methods=["POST", "GET"])
def harvest_endpoint():
    """Bulk harvest — fire and forget. Returns immediately."""
    from flask import request
    try: count = int(request.args.get("count", 10))
    except ValueError: count = 10
    count = max(1, min(count, MAX_POOL * 2))

    # pre-check: don't hammer if pool is already full
    current = _pool_size()
    if current >= MAX_POOL:
        return jsonify({"success": True, "message": "pool full", "size": current})

    pool, futures = bulk_harvest(count)
    return jsonify({
        "success": True,
        "message": f"queued {count} harvests",
        "concurrency": MAX_WORKERS,
        "pool_size_before": current,
    })

@app.route("/pool", methods=["GET"])
def pool_status():
    with _pool_lock:
        pool = _purge_stale(_load_pool())
        _save_pool(pool)
    with _stats_lock: s = dict(_stats)
    return jsonify({
        "size": len(pool),
        "ttl_sec": TOKEN_TTL_SEC,
        "target": TARGET_POOL,
        "max": MAX_POOL,
        "workers": WORKERS,
        "max_workers": MAX_WORKERS,
        "stats": s,
        "tokens": [
            {"created_at": t["created_at"], "age_sec": _now() - t["created_at"]}
            for t in pool
        ],
    })

@app.route("/purge", methods=["POST"])
def force_purge():
    with _pool_lock:
        before = _load_pool()
        after = _purge_stale(before)
        _save_pool(after)
    return jsonify({"success": True, "removed": len(before) - len(after),
                    "remaining": len(after)})

if __name__ == "__main__":
    start_refiller()
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
