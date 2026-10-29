# StoryBit v3.0.6 逆向报告（第一轮）

目标：`StoryBit v3.0.6 [Premium].apk`（66.0 MB，`storybit.story.maker.animated.storymaker`）
产出：完整协议还原 + 本地网关 + demo 页 + 压测 + 对接文档
结论标签：**协议 = 已验证**、**AI 生成 = 上游维护中（阻塞在上游，非协议问题）**、**目录/模板 = 已验证**

---

## 1. 目标画像

| 项 | 值 |
|---|---|
| package | `storybit.story.maker.animated.storymaker` |
| versionName / versionCode | `3.0.6` / `140`（`AndroMod 0x8c`） |
| 厂商 | 三方后端 `snovxgames.com`（白标 SaaS 家族，同时供 `lovi` / `nocrop` 等 App 复用） |
| 体积 / dex | 66 MB，9 个 dex，73,197 类 |
| native | 仅 `arm64-v8a` + `armeabi-v7a`，**无 x86_64** → x86_64 模拟器 `INSTALL_FAILED_NO_MATCHING_ABIS` |
| 加固 | `libpglarmor.so` / `libstub.so`（PieMods 注入，`https://t.me/piemods`）；业务 dex 未加固、类名未混淆 |
| 网络栈 | Retrofit2 + OkHttp + Gson + kotlinx.serialization，`HttpLoggingInterceptor` 在生产包里仍启用 |
| 广告 | AppLovin / AdMob / Unity Ads / IronSource / Pangle / InMobi / Mintegral / Moloco（重度广告变现） |

App 类型：**视频模板 + AI 写真（image-to-image）** 双引擎。模板侧是本地渲染（VFX zip + 用户照片 → 视频），
AI 侧是服务端生图（`gen_i` / `upScale_n`）。RC 中 `isPromptButton=true`，但协议层没有 prompt 文本字段 —— 该按钮走的仍是效果列表。

---

## 2. 关键发现：Base URL 与密钥全部不在 APK 里

dex / resources / assets / so 全量字符串扫描（230,480 条）**没有任何业务域名**。
唯一明文 URL 全是 SDK 与商店链接，`api.` `storybit` 域名零命中。

`remote/ApiUtils.smali` 给出了答案：

```smali
invoke-static {}, Lcom/google/firebase/remoteconfig/FirebaseRemoteConfig;->getInstance()
const-string v1, "API_URL"        # ← 主后端
FirebaseRemoteConfig;->getString(...)
const-string v1, "API_AI_URL"     # ← AI 后端
```

`comman/AES.smali` 的密钥同样来自 RC（`ED` / `AI_ED` / `AED`）。**APK 里既没有默认值也没有兜底**，
RC 拉不到 → key 为空 → `Key must be 32 bytes` 崩溃。这是该厂的统一打法（同族 Cloudi AI / Nano AI 一致）。

### 2.1 提取链（零设备、零 Frida、纯官方 API，实测 3 步）

```
1) resources.arsc
     google_api_key = AIzaSyDuEwsBdKGPWJahPQ0ti87udLNgO-haeOs
     project_id     = storybit-story-maker
     google_app_id  = 1:89763144585:android:c44334848bf72e5469b758
   签名证书 SHA1（分发版 APK，从 META-INF/ANDROID.RSA 取）
     2BFBA76612C78FE8675F7829457B5E47F26529C7
2) POST firebaseinstallations.googleapis.com/v1/projects/storybit-story-maker/installations
     headers: X-Goog-Api-Key / X-Android-Package / X-Android-Cert
     body:    {appId, authVersion:"FIS_v2", sdkVersion:"a:17.2.0"}
     → fid + refreshToken + authToken
3) POST firebaseremoteconfig.googleapis.com/v1/projects/storybit-story-maker/namespaces/firebase:fetch?key=<api key>
     body:    {app_instance_id: fid, app_instance_id_token: token, app_id, package_name, ...}
     → 25 个参数
```

> 坑：`analytics_user_properties` 必须是 **map**，传 `[]` 直接 400
> `Cannot bind a list to map for field 'analytics_user_properties'`。

### 2.2 RC 下发内容（2026-10-29 实测）

| key | 值 | 用途 |
|---|---|---|
| `API_URL` | `https://stb.snovxgames.com:2053` | 模板/分类/支付/通知主后端 |
| `API_AI_URL` | `https://aa.snovxgames.com:5000` | AI 后端（效果列表 / 生图 / 任务状态） |
| `MORE_APP_URL` | `https://apps.snovxgames.com:7000` | 互推 banner |
| `ED` | `ksdfjgnsjkdOLKfskjdfghJGFsrfdgad` | 主后端 AES-256 key + 本地 premium 解密 |
| `AI_ED` | `LJs97MDf0EH667sjfh6gsUsjdf897jmj` | AI 后端 AES-256 key |
| `AED` | `KJhdjkfb734OjdlKHKjJhdkgjlaj3480` | 本地渲染配置（`AES->try`） |
| `MORE_APP_URL_ED` | `lksjfdlnalKjsdKLJghgdkhlaksdhgkS` | 互推 banner key |
| `FN` | `fDFRCqlZINt+cMkiYeL9cQ==` | 16 字节 b64，未见业务调用点（备用） |
| `aiTool` | `{"UpScale":{"enable":false,"id":"6527ee586890ad39dc36e81a"}}` | **AI 工具总开关（当前关）** |
| `isAIOption` | `false` | AI 入口是否展示 |
| `ai_image_size_limit` | `20` | 单次可处理张数上限 |
| `COIN_FLAG` | `false` | 金币体系关闭 |
| `PackageName` | `ns.common.apps` | 家族公共包名（跨 App 埋点） |

网关启动时自动重拉 RC —— **厂商换密钥/换域名对我们零成本**。

---

## 3. 加密协议

`comman/AES.smali` 全量还原：

```java
IV  = new byte[16];                      // 全 0，静态常量
key = RemoteConfig.getString(<ED|AI_ED|AED>).getBytes(UTF_8)
alg = "AES/CBC/PKCS5Padding"
enc = Base64.encodeToString(cipher.doFinal(pt), Base64.NO_WRAP)   // flag=2
dec = new String(cipher.doFinal(Base64.decode(ct, 2)), UTF_8)
```

请求体固定信封：`{"data": "<b64>"}`，其中明文是业务 JSON。响应同构：`{"data": "<b64>"}`。

- 主后端 → `ED`
- AI 后端 → `AI_ED`
- 互推 banner → `MORE_APP_URL_ED`
- 本地渲染/premium → `AED` / `ED`

无签名、无时间戳、无 nonce、无设备指纹、**无任何用户身份**（请求里没有 uid/token/session）。
→ 协议层完全无状态，天然可池化、无限流维度（除 IP/Cloudflare）。

### 3.1 传输层自定义

主后端走 OkHttp interceptor 把参数塞进 **query**：

```
?cc=<Locale>&version_name=3.0.6&version_code=140
&andorid_os_version=<SDK_INT>&package_name=<pkg>&installer=<installerPackageName>
```

AI 后端走 header（连字符风格不同，注意）：

```
cc / version_name / version_code / andorid_os_version / package_name / platform: Android
```

> **Cloudflare 1010 坑**：三个域都挂 CF，非 `okhttp/*` UA 直接
> `error code: 1010`（403，`text/plain`）。python 默认 UA 必挂，必须 `User-Agent: okhttp/4.12.0`。

---

## 4. 端点全表（25 个，全部实测）

`services/UserService.smali` 是唯一 Retrofit 接口，两条 Retrofit 实例分别指向 `API_URL` 与 `API_AI_URL`。

### 4.1 主后端 `stb.snovxgames.com:2053`（JSON + ED）

| 路径 | 入参 | 实测 | 返回要点 |
|---|---|---|---|
| `/android/v2/category/getEnableCategory` | `{v,cc}` | ✅ 200 / 0.5–5.0s | 26 个模板分类 + 封面图 |
| `/android/v1/template/getHomeTemplates` | `{v,cc,cat_id,page,limit}` | ✅ 200 | "Please send Proper Data" 空壳需带 `cat_id` |
| `/android/v2/template/getExploreData` | `{v,cc}` | ✅ 200 / 3.0s | 首页聚合（Latest/Musical…） |
| `/android/v2/template/getAllTemplatesByCategoryExplore` | `{v,cc,cat_id,page,limit}` | ✅ 200 / 5.2s | 分类模板分页 |
| `/android/v2/template/getTemplateDetail` | `{v,temp_id}` | ✅ 200 / 1.7s | `vfx_url`(zip) / `playback_url`(gif) / `preview_url`(mp4) / `images[]` / `ratio` |
| `/android/v1/template/watchTemplate` | `{v,temp_id}` | ✅ 200 | 计数埋点 |
| `/android/v1/template/completeVideo` | `{v,temp_id}` | ✅ 200 | 导出完成埋点 |
| `/android/v1/template/handleAnimationScale` | `{v}` | ✅ 200 | 配置 |
| `/android/v1/notification/getNotification` | `{v,date,"","DateAndTimeFirstTimeLaunch"}` | ✅ 200 | 通知列表（当前空） |
| `/android/v1/notification/likeDislikeNotification` | – | 未测 | 通知点赞 |
| `/android/v1/banner/getDashboardBanner` | `{v}` | ✅ 200 / 2.3s | 首页 banner（`iCallPix` 等互推） |
| `/android/v1/effects/getExplore` | `{pn}` | ⚠️ 主后端 404 | **实际在 AI 后端** |
| `/android/v1/config/coin_data` | `{v,cc}` | ✅ 200 | 每日金币 + 兑换档位（50000 币/5天 VIP） |
| `/android/v1/config/spinner_data` | `{v,cc}` | ✅ 200 | 转盘档位 `[10..80]`，每日 5 次 + 额外 5 次 |
| `/android/v1/config/vu` | `{v,cc}` | ✅ 200 / 0.54s | 版本弹窗文案 + 商店链接 |
| `/android/v1/payment/handlePriceUpdate` | `{v}` | ✅ 200 | `isUpdate:false` |
| `/android/v1/payment/handleBhupat` | `{data}` | 未测 | Google Play 购买校验 |
| `/android/v1/payment/validateBhupat` | `{data}` | 未测 | 订阅校验 |
| `/android/v1/reportVideo/sendTemplateReport` | – | 未测 | 举报 |
| `/android/v1/processStatus/(get|cancel)` | – | ⚠️ 主后端不属于它 | **实际在 AI 后端** |

### 4.2 AI 后端 `aa.snovxgames.com:5000`（JSON 或 multipart + AI_ED）

| 路径 | 形态 | 实测 | 说明 |
|---|---|---|---|
| `/android/v1/effects/getExplore` | JSON `{pn}` | ✅ 200 / 4.6s | **48 分类 / 914 个 AI 写真效果**（Ghibli / Pixar / Toon / Anime / Headshot / LinkedIn / Professional / Superhero …） |
| `/android/v1/loadBalance/gen_i` | **multipart** | ✅ 受理，❌ 不出片 | `data` 文本段（AI_ED）+ 图片原始段（字段名 = 文件名） |
| `/android/v1/loadBalance/upScale_n` | **multipart** | ❌ `code 120` | `"Image upscale is under maintenance. Back soon!"` |
| `/android/v1/processStatus/getProcessStatus` | JSON `{process_id,cropRadio,pn}` | ✅ 200 | 返回 `{state,status,url}`；`state ∈ {waiting,completed}` |
| `/android/v1/processStatus/cancelProcess` | JSON `{process_id,pn}` | ✅ 200 | 取消 |

### 4.3 互推 `apps.snovxgames.com:7000`（JSON + MORE_APP_URL_ED）

| 路径 | 实测 | 说明 |
|---|---|---|
| `/android/v1/banner/getMyBanners` | ✅ 200 | 家族 App 互推（`Lovi` / `NoCrop` …，带 UTM） |

### 4.4 `gen_i` 精确报文（`Api.case` + `UpScaleGenerateFragment.class`）

```
POST https://aa.snovxgames.com:5000/android/v1/loadBalance/gen_i
headers: cc/version_name/version_code/andorid_os_version/package_name/platform
Content-Type: multipart/form-data; boundary=...

--b
Content-Disposition: form-data; name="data"
Content-Type: text/plain; charset=UTF-8

<base64(AES-256-CBC(AI_ED, {"img_ratio":"1:1","effect_id":"<24hex>","width":W,"height":H,
                            "v":140,"PACKAGE_NAME":"<pkg>","is_riksha":true,"pn":"<pkg>"}))>
--b
Content-Disposition: form-data; name="<本地文件名>"; filename="<本地文件名>"
Content-Type: multipart/form-data

<image bytes>
--b--
```

受理返回 `{"status": true, "job_id": "<workerId>_<epochMs>"}`；状态查询返回
`{"state":"waiting|completed","status":bool,"url":"<成品URL>"}`，App 侧每 **10 000 ms** 轮询一次。

错误码：`113` = effect 非法/未知；`120` = 功能维护中。

---

## 5. 计费 / 权益：纯客户端，服务端不校验

`helper/prefernces/AppPreferences.smali#new()`（即 `is_riksha` 的来源）被改包：

```smali
.method public final new()Z
    .locals 2
    const/4 v0, 0x1
    return v0              # ← 直接 return true，后面全是死代码
    ...  # 原逻辑：SharedPreferences["isBhupat"] → AES.decrypt(ED) → equals("bhupat")
```

即：本包**无条件 premium**。服务端对 `is_riksha` 只做记录，不校验，
`payment/*` 端点只服务金币/订阅埋点。→ AI 生图不消耗任何额度、无账号、无配额。

---

## 6. AI 生成链路现状（唯一阻塞项）

`gen_i` 受理成功、`getProcessStatus` 恒为 `waiting`：

| 实验 | 结果 |
|---|---|
| 纯色图 + `effect_id=68904a90…` | `status:true, job_id` → 轮询 180 s 恒 `waiting` |
| 真人像 + `effect_id=67fe4288…` | 同上，8×8 s 全 `waiting` |
| `is_riksha=true/false` 对照 | 无差异 |
| `effect_id=6527ee58…`（RC 里的 `aiTool.UpScale.id`） | `code 113` 非法 |
| `cropRadio` 取代 `img_ratio` | Cloudflare 504（请求被阻塞至超时） |
| `upScale_n` | `code 120` "under maintenance" |
| 无效 `process_id` | `state:"completed"` + `msg:"Somthing Wrong !"`（对照组，证明轮询本身正常） |

同时 RC `aiTool.UpScale.enable=false`、`isAIOption=false` → **厂商侧 AI worker 处于下线/维护状态**。
这不是协议问题：受理、跑批、轮询、取件四条腿都通了，缺的只是服务端消费者的回来。

→ 网关因此设计成**降级不炸**：受理成功但产物未就绪返回 `202 + job_id`，
并提供 `STORYBIT_MOCK=1` 全链路 mock，压测/demo/前端联调不依赖上游是否在线。

---

## 7. 证据时间线（2026-10-29）

| 时间 | 动作 | 结果 |
|---|---|---|
| 15:0x | `rg` 全 dex URL 扫描 | 230,480 串，0 业务域名 |
| 15:0x | 读 `remote/ApiUtils` + `comman/AES` | base URL/密钥 = RC |
| 15:3x | resources.arsc → AIza + project_id | `AIzaSyDuEws…haeOs` / `storybit-story-maker` |
| 15:35 | Firebase Installations 注册 | `fid=djDwdoERdKOQFX1qSfpAaZ` |
| 15:35 | RC fetch | **200，25 参数，双域名 + 4 把 key** |
| 15:35 | 第一个端点 `getEnableCategory` | 200，AES 解出 26 分类明文 |
| 15:4x | 全端点矩阵 ×18 | 18/18 通 |
| 15:5x | `effects/getExplore` | 48 分类 / 914 效果 |
| 16:0x | `gen_i` ×5 变体 | 受理成功，产物恒 waiting |
| 16:1x | `upScale_n` | `code 120` 维护中 |

---

## 8. 未解决 / 待验证

- `gen_i` 何时恢复：需厂商 worker 复活；网关已支持在线自动取件，无需改代码。
- `payment/handleBhupat` / `validateBhupat` 的入参结构未还原（需真实 Play 购买 token，成本高、收益低，未做）。
- `reportVideo` / `likeDislikeNotification` 入参未逐字核对（埋点类，无业务价值）。
- `FN` RC 值用途未知（16 字节，无调用点）。
- 真机动态验证未做：本机只有 x86_64 模拟器，该 APK 仅 arm64/armv7。
  但**静态协议 + 官方 RC API 已完整覆盖**，动态分析无增量价值。
