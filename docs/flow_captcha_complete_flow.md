# Flow Captcha Service 完整处理流程文档

> 用途：对比排查改造版本的差异，文档基于原版源码逐行分析生成。

---

## 一、项目启动流程

### 1.1 入口 `src/main.py`

```
FastAPI lifespan 启动：
  → db.init_db()              # 初始化数据库
  → runtime.start()           # 启动 CaptchaRuntime
  → cluster_manager.start()   # 启动集群管理（若有）
```

### 1.2 `CaptchaRuntime.start()` — `captcha_runtime.py:22-31`

- 启动 `_cleanup_task`（会话清理循环，每 30 秒执行一次）
- 如果不是 master 节点，调用 `service.warmup_browser_slots()`

### 1.3 浏览器数量配置 — `browser_captcha.py:1789-1801`

```python
# 从数据库读取，默认值 = 1
self._browser_count = max(1, captcha_config.browser_count)

# 根据数量创建并发信号量（并发上限 = 浏览器数量）
self._token_semaphore = asyncio.Semaphore(self._browser_count)
```

> **浏览器数量由数据库配置决定，`config/setting_example.toml` 中默认 `browser_count = 1`**

### 1.4 Warmup 预热 — `browser_captcha.py:1852-1855`

```python
# 并行预热所有 slot
tasks = [self._warmup_browser_slot(browser_id) for browser_id in range(self._browser_count)]
await asyncio.gather(*tasks, return_exceptions=True)
```

每个 slot 调用 `_get_or_create_shared_browser()` 提前把浏览器拉起来备用。

---

## 二、浏览器初始化详情

### 2.1 启动参数 — `browser_captcha.py:641-711`

```python
browser_args = [
    '--disable-blink-features=AutomationControlled',  # 隐藏自动化特征（关键）
    '--disable-quic',
    '--disable-features=UseDnsHttpsSvcb',
    '--no-sandbox',
    '--disable-dev-shm-usage',
    '--disable-setuid-sandbox',
    '--no-first-run',
    '--no-zygote',
    f'--window-size={width},{height}',
    '--disable-infobars',
    '--hide-scrollbars',
]

# 后台模式额外参数（browser_launch_background = true 时生效）
if launch_in_background:
    browser_args.extend([
        '--start-minimized',
        '--disable-background-timer-throttling',
        '--disable-renderer-backgrounding',
        '--disable-backgrounding-occluded-windows',
        f'--flow2api-browser-slot={self.token_id}',  # 标记 slot 编号
    ])
    # Windows 下移到屏幕外
    browser_args.append('--window-position=-32000,-32000')
```

### 2.2 指纹参数初始化 — `browser_captcha.py:443-450`

```python
def _refresh_browser_profile(self):
    base_w, base_h = random.choice(self.RESOLUTIONS)       # 从 28 种分辨率中随机选
    self._profile_user_agent = random.choice(self.UA_LIST)  # 从 67 个 UA 中随机选
    self._profile_viewport = {
        "width": base_w,
        "height": base_h - random.randint(0, 80),           # 高度随机偏移 0~80px
    }
```

**UA 池**：67 个，涵盖 Chrome 128-132、Edge、Firefox 128-134、Safari、Android/iPhone 移动端（更新至 2026-03-01）

**分辨率池**：28 种，从 1024x768 到 3840x2160

### 2.3 浏览器与 Context 创建 — `browser_captcha.py:695-711`

```python
browser = await playwright.chromium.launch(
    headless=False,          # 有头模式（非 headless）
    executable_path=...,     # 可配置自定义 Chromium 路径
    proxy=proxy_option,      # 可选代理
    args=browser_args,
)
context = await browser.new_context(
    user_agent=random_ua,    # 随机 UA
    viewport=viewport,       # 随机分辨率
)
```

### 2.4 初始指纹记录 — `browser_captcha.py:656-659`

```python
self._last_fingerprint = {
    "user_agent": random_ua,
    "proxy_url": raw_proxy_url if raw_proxy_url else None,
}
```

---

## 三、打码请求处理流程

### 3.1 HTTP 入口 — `src/api/service.py:53-144`

```
POST /api/v1/solve
  ↓
  验证 API Key（verify_service_api_key）
  ↓
  检查 project 是否可用 → 不可用则返回 HTTP 403
  ↓
  消耗配额             → 配额不足则返回 HTTP 403
  ↓
  路由判断：
    cluster master → cluster.dispatch_solve()  （转发给子节点）
    本地节点       → runtime.solve()            （本地执行）
```

### 3.2 `CaptchaRuntime.solve()` — `captcha_runtime.py:43-72`

```python
service = await self._get_browser_service()

# 核心：获取 token，同时返回使用的 browser_id
token, browser_id = await service.get_token(project_id, action, token_id=token_id)

# 从浏览器获取指纹快照
fingerprint = await service.get_fingerprint(browser_id)

# 注册会话（用于后续 consume/report_error）
session_id = str(uuid.uuid4())
await self.registry.create(session_id, browser_id, api_key_id, project_id, action)

return {
    "session_id": session_id,
    "token": token,
    "fingerprint": fingerprint,
    "node_name": config.node_name,
    "expires_in_seconds": config.session_ttl_seconds,  # 默认 1200 秒
}
```

### 3.3 浏览器选择策略 — `browser_captcha.py:1857-1893`

| 优先级 | 策略 | 说明 |
|--------|------|------|
| 1 | **Project 亲和性** | 优先选上次处理该 project 的 slot |
| 2 | **空闲轮询** | 若无亲和记录，按轮询选空闲 slot |
| 3 | **信号量限制** | `async with self._token_semaphore` 控制并发 ≤ browser_count |

---

## 四、浏览器内打码步骤（核心流程）

`TokenBrowser._execute_captcha()` — `browser_captcha.py:1149-1289`

### 步骤详解

| # | 操作 | 超时 | 说明 |
|---|------|------|------|
| 1 | 创建新页面 | — | `context.new_page()` |
| 2 | 注入 init script | — | `navigator.webdriver = undefined`，防检测 |
| 3 | 设置路由拦截 | — | 拦截目标页 URL，动态注入加载 enterprise.js 的 HTML |
| 4 | 监听响应事件 | — | 监听 `/recaptcha/enterprise/reload` 和 `/enterprise/clr` 200 响应 |
| 5 | 导航到目标页 | 30s | `page.goto("https://labs.google/fx/tools/flow/project/{project_id}")` |
| 6 | 等待 grecaptcha | 15s | `wait_for_function("typeof grecaptcha !== 'undefined'")` |
| 7 | **采集指纹** | — | `_capture_page_fingerprint(page)` |
| 8 | **执行打码** | 30s | `grecaptcha.enterprise.execute(website_key, {action})` |
| 9 | **等待 reload 事件** | 12s | 等待 `/enterprise/reload` 返回 200，超时返回 None |
| 10 | **等待 clr 事件** | 12s | 等待 `/enterprise/clr` 返回 200，超时返回 None |
| 11 | **额外 settle 等待** | — | 默认 **3 秒**（`browser_recaptcha_settle_seconds`） |
| 12 | 返回 token | — | |

### 步骤 3 路由拦截代码 — `browser_captcha.py:1163-1186`

```python
async def handle_route(route):
    if route.request.url.rstrip('/') == page_url.rstrip('/'):
        # 动态注入 enterprise.js，支持主备两个 host
        html = f"""<html><head><script>
        (() => {{
            const urls = [
                '{primary_host}/recaptcha/enterprise.js?render={website_key}',
                '{secondary_host}/recaptcha/enterprise.js?render={website_key}'
            ];
            const loadScript = (index) => {{
                if (index >= urls.length) return;
                const script = document.createElement('script');
                script.src = urls[index];
                script.async = true;
                script.onerror = () => loadScript(index + 1);  // 主 host 失败自动切备用
                document.head.appendChild(script);
            }};
            loadScript(0);
        }})();
        </script></head><body></body></html>"""
        await route.fulfill(status=200, content_type="text/html", body=html)
    elif any(d in route.request.url for d in ["google.com", "gstatic.com", "recaptcha.net"]):
        await route.continue_()   # 放行 reCAPTCHA 相关域名
    else:
        await route.abort()       # 拦截其他所有请求
```

### 步骤 8 打码执行代码 — `browser_captcha.py:1240-1252`

```python
token = await asyncio.wait_for(
    page.evaluate(f"""
        (actionName) => {{
            return new Promise((resolve, reject) => {{
                const timeout = setTimeout(() => reject(new Error('timeout')), 25000);
                grecaptcha.enterprise.execute('{website_key}', {{action: actionName}})
                    .then(t => {{ clearTimeout(timeout); resolve(t); }})
                    .catch(e => {{ clearTimeout(timeout); reject(e); }});
            }});
        }}
    """, action),
    timeout=30
)
```

> **关键点**：token 拿到之后，必须等到 `/reload` 和 `/clr` 两个接口都返回 200，3 秒 settle 才开始计时。缺少任一等待，则 token 可能无效。

---

## 五、指纹采集内容

`_capture_page_fingerprint()` — `browser_captcha.py:805-850`

在浏览器页面内执行 JavaScript 提取：

| 字段 | JS 来源 | 说明 |
|------|---------|------|
| `user_agent` | `navigator.userAgent` | 完整 UA 字符串 |
| `accept_language` | `navigator.language` | 浏览器语言 |
| `sec_ch_ua` | `navigator.userAgentData.brands` | Client Hints UA |
| `sec_ch_ua_mobile` | `navigator.userAgentData.mobile` | 是否移动端 |
| `sec_ch_ua_platform` | `navigator.userAgentData.platform` | 操作系统平台 |
| `proxy_url` | 内部记录 | **不对外返回**（sanitized 时移除） |

指纹采集时机：**在 `grecaptcha` 可用之后、`execute()` 调用之前**（步骤 7）

---

## 六、重试与容错机制

### 6.1 内部重试循环 — `browser_captcha.py:1512-1555`

```
max_retries = 3
每次重试间隔 = 1 秒

每次失败：
  consecutive_browser_failures + 1
  失败计数 >= 2 → recycle_browser(rotate_profile=False)  ← 不换指纹

浏览器崩溃/关闭 异常：
  → recycle_browser(rotate_profile=False)  ← 不换指纹
```

### 6.2 外部报错触发指纹切换 — `browser_captcha.py:2136-2164`

调用方调用 `report_error()` 接口，且错误信息包含以下关键词时触发：

```
"recaptcha" AND ("evaluation failed" OR "verification failed" OR "验证失败" OR "failed")
→ recycle_browser(rotate_profile=True)   # 重新随机选 UA + 分辨率
→ api_403 计数 + 1
```

### 6.3 浏览器回收条件总结

| 触发场景 | `rotate_profile` | 换指纹？ |
|---------|-----------------|---------|
| 外部调用 `report_error()` + recaptcha 失败关键词 | `True` | ✅ 换 |
| 内部连续失败 >= 2 次 | `False` | ❌ 不换 |
| 浏览器崩溃/连接关闭异常 | `False` | ❌ 不换 |
| 空闲超过 `browser_idle_ttl_seconds` | `False` | ❌ 不换 |

### 6.4 后台清理机制

| 机制 | 执行周期 | 触发条件 | 文件位置 |
|------|---------|---------|---------|
| 会话过期清理 | 每 30 秒 | 会话超 `session_ttl_seconds` | `captcha_runtime.py:194-240` |
| 浏览器空闲回收 | 每 15 秒 | 空闲超 `browser_idle_ttl_seconds` | `browser_captcha.py:1736-1758` |

---

## 七、关键配置参数对照

`config/setting_example.toml`：

```toml
[captcha]
browser_count = 1                      # 浏览器数量 = 并发上限
browser_proxy_enabled = false          # 全局代理开关
browser_proxy_url = ""                 # 全局代理地址（支持代理池，换行分隔）
browser_launch_background = true       # 是否后台启动（移到屏幕外）
browser_score_dom_wait_seconds = 25    # score 模式 DOM 等待时间
browser_recaptcha_settle_seconds = 3   # token 返回前额外 settle 等待（秒）
browser_score_test_warmup_seconds = 12 # score 模式预热等待
browser_idle_ttl_seconds = 600         # 空闲超过 10 分钟回收浏览器
flow_timeout = 300                     # Flow 接口超时
upsample_timeout = 300                 # Upsample 接口超时
session_ttl_seconds = 1200             # 会话最大生命周期 20 分钟
```

---

## 八、完整数据流示意

```
HTTP POST /api/v1/solve
    │
    ▼
验证 API Key + 检查配额
    │
    ▼
CaptchaRuntime.solve()
    │
    ├─ 选择浏览器 slot（亲和性 → 轮询）
    │
    ▼
BrowserCaptchaService.get_token()
    │
    ├─ 信号量限流（≤ browser_count 并发）
    │
    ▼
TokenBrowser.get_token()  【最多 3 次重试，间隔 1s】
    │
    ▼
_get_or_create_shared_browser()
    │
    ├─ 若无浏览器 → _create_browser()
    │     ├─ 随机选 UA（67 个）
    │     ├─ 随机选分辨率（28 种）
    │     ├─ 启动 Chromium（有头 + 12+ flags）
    │     └─ 创建 Context（注入 UA + viewport）
    │
    ▼
_execute_captcha()
    │
    ├─ 新建 page
    ├─ 注入 navigator.webdriver = undefined
    ├─ 路由拦截 → 动态注入 enterprise.js HTML
    ├─ 监听 /reload 和 /clr 响应事件
    ├─ page.goto(target_url)                    [超时 30s]
    ├─ 等待 grecaptcha 加载完毕                 [超时 15s]
    ├─ _capture_page_fingerprint()              ← 采集指纹
    ├─ grecaptcha.enterprise.execute()          [超时 30s]
    ├─ 等待 /enterprise/reload 200              [超时 12s]
    ├─ 等待 /enterprise/clr 200                 [超时 12s]
    └─ asyncio.sleep(settle_seconds=3)
         │
         ▼
       return token
    │
    ▼
get_fingerprint(browser_id)
    │
    └─ 返回：{user_agent, accept_language, sec_ch_ua, sec_ch_ua_mobile, sec_ch_ua_platform}
    │
    ▼
HTTP Response:
  {
    "session_id": "...",
    "token": "03AFcWeA...",
    "fingerprint": { ... },
    "node_name": "...",
    "expires_in_seconds": 1200
  }
```

---

## 九、对比排查重点清单

改造版本时，以下几点最容易出错，逐项核对：

- [ ] **`_execute_captcha` 中 `/reload` 事件等待**：是否还有？超时是否 12s？
- [ ] **`_execute_captcha` 中 `/clr` 事件等待**：是否还有？超时是否 12s？
- [ ] **路由拦截的 HTML 动态注入**：enterprise.js 是否被正确注入？主备 host 逻辑是否保留？
- [ ] **`browser_recaptcha_settle_seconds` settle 等待**：是否被删掉或改短？
- [ ] **信号量并发控制 `_token_semaphore`**：是否还在？
- [ ] **指纹采集时机**：是在 `wait_for_function(grecaptcha)` 之后、`execute()` 调用之前吗？
- [ ] **`navigator.webdriver` 注入**：init script 是否保留？
- [ ] **浏览器启动参数**：`--disable-blink-features=AutomationControlled` 是否保留？
- [ ] **Context 创建**：`user_agent` 和 `viewport` 是否都传入了？
- [ ] **有头模式**：`headless=False` 是否被改成了 `True`？
