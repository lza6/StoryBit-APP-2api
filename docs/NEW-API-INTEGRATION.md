# StoryBit 2API → new-api 渠道对接指南

本网关把 StoryBit v3.0.6 的私有协议包装成 **OpenAI 兼容图像接口**，可直接作为 new-api 的
`OpenAI` 类型渠道接入。全部调用无账号、无 key、无额度，成本只在 IP 维度（Cloudflare）。

---

## 0. 前置：先跑起来

```powershell
cd D:\apk-analysis\storybit\2api
pip install -r requirements.txt

# 生产（直连上游，启动时自动从 Firebase RC 拉域名+密钥）
python server.py                 # 127.0.0.1:8089

# 冒烟
curl http://127.0.0.1:8089/health
```

`/health` 里三个必须看的字段：

| 字段 | 期望 | 含义 |
|---|---|---|
| `rc_source` | `remote-config` | 域名/密钥来自线上 RC（不是 fallback） |
| `upstream` | `ok` | 主后端连通（含 2s 级延迟） |
| `mock` | `false` | 生产模式；`true` 时是假数据 |

> 若 `rc_source=fallback`：Firebase 被墙或 cert/key 变了 → 检查
> `python -c "import rc;print(rc.fetch())"`。

---

## 1. new-api 渠道配置（字段级）

| 字段 | 值 |
|---|---|
| 类型 | `OpenAI` |
| 名称 | `StoryBit-AI` |
| Base URL | `http://127.0.0.1:8089`（**不要加 `/v1`**，new-api 会自己拼） |
| 密钥 | 任意非空（如 `sk-storybit`），网关不校验 |
| 模型 | `storybit` |
| 分组 | 按你的策略 |
| 权重 | 先 1 |
| 代理 | 留空（网关自己出网；**不要**给网关挂国内代理，CF 会 1010） |

### 1.1 调用姿势：必须走 `/v1/images/edits`

StoryBit 是 **image-to-image**（必须有输入图）。new-api 的 `/v1/images/generations`
请求体里没有图片字段（`dto.ImageRequest` 只有 model/prompt/n/size/response_format…），
所以客户端要打 **`POST /v1/images/edits`**（multipart）：

```bash
curl -X POST http://<new-api>/v1/images/edits \
  -H "Authorization: Bearer <new-api token>" \
  -F "model=storybit" \
  -F "prompt=Ghibli" \
  -F "size=1:1" \
  -F "image=@face.jpg"
```

`prompt` 在这里不是提示词，而是**效果选择器**，语义按优先级：

1. 精确命中 `effect_id`（24 位 hex，见 `GET /v1/effects`）
2. 精确命中效果名（`Barbie` / `anime_8` / `linkedin_28` …）
3. 子串命中效果名或分类名（`ghibli` / `anime` / `headshot` / `pixar` / `toon` / `superhero`）

找不到会返回 400 并提示去 `/v1/effects` 浏览，不会静默降级。

> 若你的客户端只会打 `/v1/images/generations`：改成 JSON，把图片放
> `image`(base64) 或 `image_url`，`prompt`/`model` 语义与上面一致（`model=storybit/ghibli` 也支持）。

---

## 2. 定价必须设固定价（防暴扣 / 防白嫖错判）

上游不计费、无 token 概念，new-api 的按量计费对它**没有意义**，必须用固定价：

- 计费方式：**按次**（fixed price per call）
- 建议值：按你对外售价定，例如 `0.02 USD/次`（图片类）
- 不要把 `storybit` 放进按 token 计费的分组，否则 `usage.prompt_tokens` 恒为 1，
  实际等于免费。

网关返回体是标准 `{"created":…,"data":[{"url":…}]}`，
new-api 的 `ImageHelper` 能正常解析（`b64_json` 也支持，`response_format=b64_json` 时网关会代下载转码）。

---

## 3. 当前上游状态（2026-10-29 实测）

| 能力 | 状态 |
|---|---|
| 目录 / 模板 / 分类 / banner / 效果列表（914 个） | 🟢 全通 |
| `gen_i` 生图 | 🟡 **受理成功但厂商 AI worker 下线**：返回 `202 + job_id`，`state` 恒 `waiting` |
| `upScale_n` 放大 | 🔴 上游 `code 120` "under maintenance" |

⚠️ **接 new-api 前先确认上游是否恢复**：
```bash
python - <<'PY'
import httpx, base64, io
from PIL import Image
b=io.BytesIO(); Image.new("RGB",(768,768),(30,60,120)).save(b,"JPEG")
r=httpx.post("http://127.0.0.1:8089/v1/images/generations",timeout=180,json={
  "model":"storybit","prompt":"Ghibli",
  "image":base64.b64encode(b.getvalue()).decode(),"poll_timeout":60})
print(r.status_code, r.text[:300])
PY
```
- `200 + data[].url` → 上游已恢复，可直接上渠道。
- `202 + state:waiting` → 上游仍在维护。此时渠道**不要上生产**，否则每单都返回空图。

在维护窗口内做前端联调/压测，用 mock 模式：
```powershell
$env:STORYBIT_MOCK=1; python server.py
```

---

## 4. 验证四步（接完必跑）

```bash
# 1) 网关自身
curl http://127.0.0.1:8089/health

# 2) 效果目录（914 个，网关缓存 10 分钟）
curl "http://127.0.0.1:8089/v1/effects?q=ghibli" | head -c 400

# 3) 经 new-api 走 edits（注意是 multipart）
curl -X POST http://<new-api>/v1/images/edits \
  -H "Authorization: Bearer <token>" \
  -F "model=storybit" -F "prompt=Ghibli" -F "image=@face.jpg"

# 4) new-api 侧核对：日志里应看到
#    渠道=StoryBit-AI  模型=storybit  size=1:1  计费=固定价
```

### 排错表

| 现象 | 原因 | 处置 |
|---|---|---|
| `403 error code: 1010` | UA 被 CF 拦 | 网关已固定 `okhttp/4.12.0`；若你改了代码别动 UA |
| `Key must be 32 bytes` | RC 没拉到，key 空 | 检查 `/health.rc_source`，重跑 `rc.fetch()` |
| `404 .../effects/getExplore` | 打错主机 | 该端点在 AI 主机（`:5000`），网关已路由 |
| 渠道测试 200 但无图 | 上游 202 排队 | 见第 3 节，上游维护中 |
| 模板列表空 + `Please send Proper Data` | `getHomeTemplates` 缺 `cat_id` | 先取 `/v1/categories` 再带 `cat_id` |
| 并发一高就 504 | Cloudflare 100s 上限 + 上游慢 | 网关默认 8 并发闸门（`STORYBIT_MAX_CONCURRENCY`），别调太高 |

---

## 5. 网关接口一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 存活 + RC 来源 + 上游连通 + 计数器 |
| GET | `/v1/models` | 两个虚拟模型 `storybit` / `storybit-upscale` |
| POST | `/v1/images/generations` | OpenAI 兼容（JSON，图走 `image`/`image_url`） |
| POST | `/v1/images/edits` | OpenAI 兼容（multipart，图走 `file`）← **new-api 用这个** |
| POST | `/v1/upscale` | 上游维护中 |
| GET | `/v1/jobs/{id}` | 轮询 AI 任务（直接透上游 state） |
| DELETE | `/v1/jobs/{id}` | 取消 |
| GET | `/v1/effects/categories` | 48 个 AI 效果分类 |
| GET | `/v1/effects?q=&category=` | 914 个效果（含缩略图） |
| GET | `/v1/categories` | 26 个模板分类 |
| GET | `/v1/templates?cat_id=&page=&limit=` | 分类模板分页 |
| GET | `/v1/templates/explore` | 首页聚合 |
| GET | `/v1/templates/{id}` | 模板详情（`vfx_url` / `preview_url` / `ratio` / `images[]`） |
| GET | `/v1/banners` | 首页 banner + 家族互推 |
| GET | `/` | demo 页（可直接浏览器打开） |

---

## 6. 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `PORT` / `HOST` | `8089` / `127.0.0.1` | 监听 |
| `STORYBIT_MOCK` | `0` | `1` = 全链路 mock，不碰上游 |
| `STORYBIT_MAX_CONCURRENCY` | `8` | 同时在飞的 AI 任务数 |
| `STORYBIT_POLL_TIMEOUT` | `120` | 生成请求内最长等待；超时返回 202 |
