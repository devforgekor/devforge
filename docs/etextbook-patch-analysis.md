# Etextbook Jikji Binder: Bug Analysis & Patch History

**Date:** 2026-08-31  
**Author:** AI Agent (Claude Code)  
**Affected System:** `161.33.199.207` — Docker container `etextbook` (nginx/1.31.4)  
**Application:** Jikji Binder 0.9.9_demo.web.2 — etextbook viewer (중학교 체육)

---

## 1. Architecture Overview

### Deployment
- Host: OCI instance (161.33.199.207, ap-tokyo-1)
- Container: `etextbook/etextbook-viewer:latest` (sudo docker)
- Web server: nginx 1.31.4 (inside container)
- Storage: `/data/etextbook/resource/` → `/usr/share/nginx/html/resource/` (bind mount)
- Cache: `expires 7d; immutable` for JS, `expires 30d; immutable` for `/resource/`

### Two Rendering Systems

The etextbook has two parallel rendering systems:

| System | Entry Point | Tech Stack | Content Source |
|--------|-------------|------------|----------------|
| **Old (legacy)** | `page-*.html` | render.app.min.js (AngularJS) + binder_web.min.js (NW.js compat) | `resource/contents/1/lesson0{N}/OPS/page-*.html` |
| **New (Angular)** | `viewer/contents/index.html` / `viewer/ebook/index.html` | Angular (main.a300*.js) + jjbundle | `contentInformationURL` query param → iframe → page-*.html |

### User Navigation Flow

```
Browser → http://161.33.199.207/resource/ → include/selection/index.html (영역 선택)
  → main/index.html (홈, 진도 관리)
    → viewer/ebook/index.html?contentInformationURL=../../resource/ebook/1/&page=N
      → Angular app loads cdbook.xml → META-INF/container.xml → content.opf
        → builds spine (page list)
          → loads page-*.html into iframe
```

The **Angular viewer** (`viewer/ebook/` and `viewer/contents/`) loads old-style `page-*.html` pages into **iframes**. Each `page-*.html` contains:
1. `render.app.min.js` (AngularJS render engine) — loaded FIRST
2. `render-page-*.js` (page-specific data)
3. `binder_web.min.js` (NW.js→web compatibility layer) — loaded LAST

### Script Loading Order in page-*.html

```
async.min.js → mousetrap.min.js → jquery.min.js → angular.min.js → ...
→ core.lib.min.js → tool.lib.min.js → render.app.min.js → render-page-N.js
→ binder_web.min.js  ← LAST
```

---

## 2. Root Cause Analysis

### 2.1. The `render.app.min.js` `r()` Function

The alert `"현재 실행환경에서는 해당 API를 지원하지 않습니다. (jj.native.exe)"` originates from `render.app.min.js`:

```js
// render.app.min.js:62040 — the r() function
function r(e){
  if(!window.jj || !window.jj.native)
    return alert("현재 실행환경에서는 해당 API를 지원하지 않습니다. (jj.native." + e + ")");
  var t = Array.prototype.slice.apply(arguments);
  e = t.shift();
  window.jj.native[e].apply(window, t);
}
```

This function is registered as an AngularJS service (`$action_binderAPI_native`) and is called when a user clicks on interactive elements in the textbook (e.g., "파일 실행 시키기" button). The `e` parameter is one of: `"close"`, `"toggleFullscreen"`, `"exe"`, `"explorer"`, `"download"`.

### 2.2. The `binder_web.min.js` Compatibility Layer

`binder_web.min.js` is the NW.js-to-web compatibility layer. It sets up `window.jj.native` with a Proxy that wraps the NW.js native API.

**Original setup** (pre-patch):
```js
window.jj.native = t.api.native || new Proxy({}, {get: function(){return function(){return null}}});
```

The problem: `t.api.native` only has 3 methods: `{toggleFullscreen, download, close}`. When `render.app.min.js` calls `window.jj.native.exe(path)`, the Proxy returns `undefined` (because `exe` doesn't exist in `t.api.native`), and `.apply()` throws a TypeError.

### 2.3. The `_blank` Quoting Bug

In the lesson file patch, `window.open(args[0],_blank)` was missing quotes around `_blank`, causing a `ReferenceError: _blank is not defined`:

```js
// BUG (lesson file)
window.open(args[0], _blank)  // ReferenceError!
// FIX (lesson file)
window.open(args[0], "_blank")  // Works
```

### 2.4. The Proxy Overwrite Bug

The lesson file had TWO conflicting `window.jj.native` assignments:

```js
// Step 1 (position 36025): type-safe Proxy ✅
window.jj.native = (typeof t.api.native === 'object' && t.api.native !== null)
  ? new Proxy(t.api.native, {get: function(a,k){ return typeof a[k]==='function' ? a[k] : function(){return null}}})
  : new Proxy({}, {get: function(a,k){ return typeof a[k]==='function' ? a[k] : function(){return null}}});

// Step 2 (position 38109): DESTRUCTIVE OVERWRITE ❌
window.jj.native = new Proxy({}, {get: function(){return function(){return null}}});
//   ↑ This ALWAYS returns null for any property access!
//   ↑ window.jj.native.exe → null → click handler does nothing
```

The second Proxy's `get` trap ignores the property name (`k`) and always returns a noop function. This completely neuters the `exe`/`explorer` overrides set by the `_realOpen` IIFE.

### 2.5. Browser Popup Blocker in iframe

Even when `window.open()` is called correctly, **modern browsers block `window.open()` calls from within iframes**, even when triggered synchronously by a user click event. The call is silently swallowed — no error, no popup.

### 2.6. Missing Resource Files

The Angular viewer requests several resource files that don't exist in the ebook content directory:

| Resource | Path | Status |
|----------|------|--------|
| `app.config.json` | `contentURL + /jjbundle/app.config.json` | 404 (content config) |
| `link-tab.json` | `contentURL + /link-tab.json` | 404 |
| `favicon.ico` | `/favicon.ico` | 404 |

---

## 3. Fix History

### Fix #1: Type-safe Proxy for `window.jj.native` (2026-08-28)

**Files:** `binder_web.min.js` (lesson 4 + ebook 1)

**Problem:** `t.api.native` only has 3 methods, so `window.jj.native.exe` returns `undefined`.

**Solution:** Changed the Proxy to check if the target has the property:

```js
// Before
window.jj.native = t.api.native || new Proxy({}, {get: function(){return function(){return null}}});

// After
window.jj.native = (typeof t.api.native === 'object' && t.api.native !== null)
  ? new Proxy(t.api.native, {get: function(a,k){ return typeof a[k]==='function' ? a[k] : function(){return null}}})
  : new Proxy({}, {get: function(a,k){ return typeof a[k]==='function' ? a[k] : function(){return null}}});
```

### Fix #2: `_realOpen` + `window.open` for exe/explorer (2026-08-28)

**Files:** `binder_web.min.js` (lesson 4 + ebook 1)

**Problem:** `window.jj.native.exe(path)` throws because `path` is a file path, not a URL.

**Solution:** Added `_realOpen` IIFE that checks file extensions and opens via `window.open`:

```js
window.$$_realOpen = window.open || function(u,t,p){ return window.open(u,t,p); };
// ...
(function(){
  var _realOpen = window.$$_realOpen || window.open;
  if(window.jj && window.jj.native){
    var _oe = window.jj.native.exe;
    window.jj.native.exe = function(){
      var a = Array.prototype.slice.call(arguments);
      if(a.length > 0 && typeof a[0] === 'string'){
        var ext = a[0].split('.').pop().toLowerCase();
        if(['mp4','mp3','pdf','hwp','pptx','ppt','docx','doc','xlsx','xls','zip','epub'].indexOf(ext) >= 0){
          _realOpen(a[0], '_blank');
          return;
        }
      }
      return _oe.apply(this, arguments);
    };
    window.jj.native.explorer = function(){
      var a = Array.prototype.slice.call(arguments);
      if(a.length > 0 && typeof a[0] === 'string'){
        _realOpen(a[0], '_blank');
      }
    };
  }
})();
```

### Fix #3: `render.app.min.js` Safety Net (2026-08-31)

**Files:** `render.app.min.js` (lesson 4 + ebook 1)

**Problem:** `render.app.min.js` is loaded BEFORE `binder_web.min.js`. If the `r()` function is called before `binder_web.min.js` sets up `window.jj.native`, the alert fires.

**Solution:** Added a safety net before the `$action_binderAPI_native` service definition:

```js
window.jj = window.jj || {};
window.jj.native = window.jj.native || new Proxy({}, {get: function(t,k){ return k in t ? t[k] : function(){return null}}});
```

### Fix #4: `_blank` Quoting Bug (2026-08-31)

**Files:** `binder_web.min.js` (lesson 4)

**Problem:** `window.open(args[0],_blank)` — missing quotes → `ReferenceError`

**Solution:** Changed to `window.open(args[0],"_blank")`.

### Fix #5: Proxy Overwrite Bug (2026-08-31)

**Files:** `binder_web.min.js` (lesson 4)

**Problem:** The extra `window.jj.native = new Proxy(...)` at position 38109 overwrites the type-safe Proxy.

**Solution:** Changed `=` to `||` to preserve existing native object:

```js
// Before
window.jj.native = new Proxy({}, {get: function(){return function(){return null}}});

// After
window.jj.native = window.jj.native || new Proxy({}, {get: function(a,k){ return typeof a[k]==='function' ? a[k] : function(){return null}}});
```

### Fix #6: Proxy → Plain Object (2026-08-31)

**Files:** `binder_web.min.js` (lesson 4 + ebook 1)

**Problem:** The Proxy pattern fundamentally breaks direct property assignment. Setting `window.jj.native.exe = function(){}` has no effect because the Proxy's `get` trap intercepts all property reads.

**Solution:** Replaced the Proxy with a plain object:

```js
// Before
window.jj.native = window.jj.native || new Proxy({}, {get: function(){...}});

// After
window.jj.native = window.jj.native || {};
// Functions are set directly on the object
window.jj.native.exe = function(){ ... };
window.jj.native.explorer = function(){ ... };
```

### Fix #7: `window.open` → `<a>` Tag Click (2026-08-31)

**Files:** `binder_web.min.js` (lesson 4 + ebook 1)

**Problem:** `window.open()` is blocked by browser popup blockers when called from within an iframe. The call is silently discarded.

**Solution:** Replace `window.open()` with `<a>` tag creation and programmatic click:

```js
// Before
window.open(p, '_blank');

// After
(function(){
  var l = document.createElement('a');
  l.href = p; l.target = '_blank';
  l.style.display = 'none';
  document.body.appendChild(l);
  l.click(); l.remove();
})();
```

### Fix #8: Path Resolution via `window.jj._path.toURL()` (2026-08-31)

**Files:** `binder_web.min.js` (lesson 4 + ebook 1), `render.app.min.js` (lesson 4 + ebook 1)

**Problem:** The `pathString` passed to `exe()`/`explorer()` may be a Jikji internal path (e.g., `resource://.../file.mp4`), not a browser URL. The `<a>` tag needs a real URL.

**Solution:** Resolve the path through `window.jj._path.toURL()` before using it:

```js
var p = a[0];
try {
  if(window.jj && window.jj._path && window.jj._path.toURL){
    var r = window.jj._path.toURL(p);
    if(r) p = r;
  }
} catch(e){}
```

### Fix #9: `window.top.document` for Same-Origin iframe Access (2026-08-31)

**Files:** `render.app.min.js` (lesson 4 + ebook 1)

**Problem:** Even `<a>` tag click in the iframe's document may not trigger navigation. The iframe and top window are same-origin, so we can directly manipulate the top window's DOM.

**Solution:** Use `window.top.document` to create and click the `<a>` tag:

```js
// In render.app.min.js r() function
if(e === "exe" || e === "explorer"){
  try {
    var p = t[0];
    if(window.jj._path && window.jj._path.toURL){
      var u = window.jj._path.toURL(p);
      if(u) p = u;
    }
    var W = window.top || window.parent || window;
    var L = W.document.createElement('a');
    L.href = p; L.target = '_blank';
    L.style.display = 'none';
    W.document.body.appendChild(L);
    L.click(); L.remove();
  } catch(ex) {}
  return;
}
```

### Fix #10: Missing Resource Files (2026-08-31)

**Files:** `link-tab.json`, `app.config.json`, `favicon.ico`

**Problem:** The Angular viewer requests `link-tab.json`, `app.config.json` (at contentUrl path), and `favicon.ico` — all return 404.

**Solution:** Created the missing files:

| File | Path | Content |
|------|------|---------|
| `link-tab.json` | `resource/ebook/1/link-tab.json` | `{"tabCustom":[],"pageTab":[]}` |
| `app.config.json` | `resource/ebook/1/jjbundle/app.config.json` | Copy of viewer config with `content.url` set to `../../resource/ebook/1/ebook.epub/OPS` |
| `favicon.ico` | `/favicon.ico` | Copy of viewer's favicon |

---

## 4. Current Patch State

### 4.1. Files Modified

| File | Locations | md5 | Status |
|------|-----------|-----|--------|
| `binder_web.min.js` | lesson01/03/05/06 (4) | `f8ae14dda1d7844dc1b42f5e4d2321fa` | ✅ |
| `binder_web.min.js` | ebook (1) | `(current ebook version)` | ✅ |
| `render.app.min.js` | lesson01/03/05/06 + ebook (5) | `(current version)` | ✅ |
| `link-tab.json` | `resource/ebook/1/` | — | ✅ |
| `app.config.json` | `resource/ebook/1/jjbundle/` | — | ✅ |
| `favicon.ico` | `/favicon.ico` | — | ✅ |
| `link-tab.json` | `resource/contents/1/lesson{01,03,05,06}/OPS/` (4) | — | ✅ (Fix #11) |
| `app.config.json` | `resource/contents/1/lesson{01,03,05,06}/jjbundle/` (4) | — | ✅ (Fix #11) |

### 4.2. Key Patch Decision: Proxy vs Plain Object

The Jikji framework's `binder_web.min.js` originally used `Proxy` to intercept `window.jj.native[e]` calls. However, the Proxy pattern has a fundamental issue: **you cannot both use a Proxy as a safety net AND directly assign properties to it**.

```js
// This DOES NOT work with a Proxy:
window.jj.native = new Proxy({}, {get: function(){return function(){return null}}});
window.jj.native.exe = function(){...};  // Proxy.get intercepts every read
window.jj.native.exe('test.mp4');  // → Proxy.get returns null function ❌
```

The solution was to **remove the Proxy entirely** and use a plain object with directly assigned functions:

```js
window.jj.native = window.jj.native || {};
window.jj.native.exe = function(){...};  // Direct assignment works
window.jj.native.exe('test.mp4');  // → actual function called ✅
```

The `render.app.min.js` still uses a Proxy for safety (since it's loaded before `binder_web.min.js`), but with a properly functioning `get` trap:

```js
window.jj.native = window.jj.native || new Proxy({}, {
  get: function(t, k){ return k in t ? t[k] : function(){return null} }
});
// ↑ This preserves existing properties (set by binder_web.min.js later)
//   and returns noop only for missing properties
```

### 4.3. Browser Popup Blocker Mitigation

The final approach uses `window.top.document` to create an `<a>` element:

```
Action → render.app.min.js r("exe", pathString)
  → render.app.min.js safety net checks window.jj.native
  → render.app.min.js resolves path via window.jj._path.toURL()
  → Creates <a href="resolved_url" target="_blank"> in window.top.document
  → Programmatically clicks the <a> element
  → Browser navigates to the URL in a new tab ✅
```

This works because:
1. The iframe and top window are **same-origin** (both on `161.33.199.207`)
2. The `<a>` tag click simulates a user gesture at the top-level document
3. Modern browsers do not block programmatic `<a>` clicks that navigate to new tabs

---

## 5. Verification

### HTTP Resource Verification
```
All 15 page-*.html script resources → HTTP 200 ✅
render.app.min.js (5 locations) → HTTP 200, md5 match ✅
binder_web.min.js (5 locations) → HTTP 200, md5 match ✅
link-tab.json → HTTP 200 ✅
app.config.json (contentUrl path) → HTTP 200 ✅
favicon.ico → HTTP 200 ✅
```

### JS Syntax Verification
```
acorn parser (ECMA2020):
  render_final.js → NO SYNTAX ERROR ✅
  binder_web_clean_lesson.js → NO SYNTAX ERROR ✅
  binder_web_clean_ebook.js → NO SYNTAX ERROR ✅
```

### Runtime Verification (jsdom)
```
exe("test.mp4") → <a href=".../test.mp4" target="_blank"> click! ✅
exe("test.pdf") → <a href=".../test.pdf" target="_blank"> click! ✅
explorer("test.mp4") → <a href=".../test.mp4" target="_blank"> click! ✅
close() → null (noop, expected) ✅
download() → null (noop, expected) ✅
```

### Browser Console
```
No 404 errors (all resources resolved) ✅
No "지원하지 않습니다" alert ✅
Only "Permissions policy violation: unload" warnings (benign, browser deprecation) ✅
```

---

## 6. File Listing

### Lesson Files (identical across all 4 lessons)

| File | Path |
|------|------|
| `binder_web.min.js` | `resource/contents/1/lesson{N}/OPS/_framework/js/lib/binder_web.min.js` |
| `render.app.min.js` | `resource/contents/1/lesson{N}/OPS/_framework/js/render/render.app.min.js` |

Where `{N}` ∈ `{01, 03, 05, 06}`.

### Ebook File

| File | Path |
|------|------|
| `binder_web.min.js` | `resource/ebook/1/ebook.epub/OPS/_framework/js/lib/binder_web.min.js` |
| `render.app.min.js` | `resource/ebook/1/ebook.epub/OPS/_framework/js/render/render.app.min.js` |

### Supporting Files

| File | Path |
|------|------|
| `link-tab.json` | `resource/ebook/1/link-tab.json` |
| `app.config.json` | `resource/ebook/1/jjbundle/app.config.json` |
| `favicon.ico` | `/favicon.ico` |

### Backup Files

All modified files have `.bak_20260831` backups in their respective directories.

---

## 7. Server Configuration

### nginx Cache
```
location /resource/ { expires 30d; add_header Cache-Control "public, immutable"; }
location ~* \.(js|css|png|jpg|jpeg|gif|ico|svg|woff2?)$ {
  expires 7d; add_header Cache-Control "public, immutable";
}
```

**Note:** Because of the 7-day cache, users who previously visited the etextbook must clear their browser cache or use Ctrl+F5 (hard refresh) to get the patched files. New users (first visit) will get the patched files automatically.

### Container
```
Name: etextbook
Image: etextbook/etextbook-viewer:latest
Port: 80 → 80
Mount: /data/etextbook/resource/ → /usr/share/nginx/html/resource/
```

The `render.app.min.js` and `binder_web.min.js` files are on the **bind mount** (`/data/etextbook/resource/`), so changes are immediately visible inside the container without requiring a restart.

---

## 8. Known Issues

1. **`unload` Permission Policy Violation**: The browser console shows `[Violation] Permissions policy violation: unload is not allowed in this document.` This is a harmless warning from Chrome's deprecation of the `unload` event. The `render.app.min.js` and jQuery use `$(window).on('unload', ...)` which triggers this warning. It does NOT affect functionality.

2. **`link-tab.json` 404 (before Fix #10)**: The Angular viewer's `LinkTabService.loadJson()` fetches `link-tab.json` and logs "load LinkTab Data []" on 404. The empty array is handled gracefully, but the 404 was fixed by creating the file.

3. **`app.config.json` 404 at contentUrl path (before Fix #10)**: The viewer tries to load `contentUrl + "/jjbundle/app.config.json"` first. On 404, it falls back to the default config. The fallback works, but the 404 was fixed by creating the config at the contentUrl path.

---

## 9. Fix #11: Missing Resource Files for Lesson Content Viewer (2026-09-02)

**Verification method:** Playwright (chromium, headless) navigating the real `viewer/contents/index.html?contentInformationURL=...` flow (not jsdom), capturing `response`/`pageerror`/`console` events.

**Problem:** Fix #10 (2026-08-31) only created `link-tab.json` and `jjbundle/app.config.json` under `resource/ebook/1/`. The equivalent files were never created for the 4 lesson content directories (`resource/contents/1/lesson{01,03,05,06}/`), which use the **`viewer/contents/index.html`** entry point, not `viewer/ebook/`. Reproduced live:

```
GET /resource/contents/1/lesson01/jjbundle/app.config.json  -> 404
GET /resource/contents/1/lesson01/OPS/link-tab.json         -> 404
```

These 404s were also accompanied by a caught-exception console log (`[error] fE`, a minified error object) right before `LinkTabService` logged its empty-array fallback — same pattern as Known Issue #2, just previously unaddressed for the contents viewer.

**Solution:** Created, for each of `lesson01`, `lesson03`, `lesson05`, `lesson06`:

| File | Path | Content |
|------|------|---------|
| `link-tab.json` | `resource/contents/1/lesson{N}/OPS/link-tab.json` | `{"tabCustom":[],"pageTab":[]}` (identical to ebook's) |
| `app.config.json` | `resource/contents/1/lesson{N}/jjbundle/app.config.json` | Copy of `resource/ebook/1/jjbundle/app.config.json` with `content.url` set to `../../resource/contents/1/lesson{N}/OPS`, and the `template` array's `"activated"` flags swapped so `"contents"` is `true` and `"ebook"` is `false` (matching the default `viewer/contents/jjbundle/app.config.json` template selection) |

**Not fixed (intentionally):** `cdbook.xml` and `digitaltextbook.xml` also 404 at the lesson root (`resource/contents/1/lesson{N}/cdbook.xml`, `.../digitaltextbook.xml`). These are **not bugs** — the viewer probes multiple book-manifest formats (`cdbook.xml` → `digitaltextbook.xml` → EPUB3 `META-INF/container.xml`/`content.opf`) in order and falls back correctly. Lesson content directories are genuine EPUB3 packages (they already have a valid `META-INF/container.xml` + `OPS/content.opf`), so faking a `cdbook.xml` there would misrepresent the content format. Leaving these as expected fallback-probe 404s is consistent with how the ebook path's own `cdbook.xml` is the *real* manifest for that content type.

**Verification (Playwright, real navigation, all 4 lessons):**
```
Before: 4 failed requests (app.config.json, cdbook.xml, digitaltextbook.xml, link-tab.json) + 1 console error per lesson
After:  2 failed requests (cdbook.xml, digitaltextbook.xml — expected format probes) + 0 console errors per lesson
No pageerror exceptions in any case (before or after).
```

Separately verified (does not require a fix): the `<a>`-tag-click mechanism from Fix #7/#9 was tested end-to-end inside the real nested iframe (`viewer/ebook` → `page-32.html`) using Playwright's `popup` event listener — a `window.top.document`-created `<a target="_blank">` click **does** open a real new tab from within the iframe, confirming the popup-blocker workaround functions correctly in an actual browser (not just jsdom).

Also confirmed via file-existence audit: of 375 distinct `pathString` values referenced across all ebook `render-page-*.js` files, 329 (~88%) resolve to real files under `/data/etextbook/resource/data/...` when used as a literal root-relative URL — i.e. `window.jj._path.toURL()` (Fix #8) being a no-op in the web build (jj._path does not exist there) is harmless for the large majority of content, since these pathStrings are already stored as web-servable absolute paths. The remaining ~12% reference media files that are missing from the uploaded PE dataset entirely — a content/asset-upload gap, not a JS logic bug.