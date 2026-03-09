# AI-Vtuber 使用手册（商用版）

## 1. 产品定位

`AI-Vtuber` 是一个面向直播场景的实时互动工具，提供：

- 弹幕实时回复（多模型容灾）
- 语音合成播报（Edge TTS / Vocu）
- 外部音频源实时字幕（ASR + 翻译）
- OBS 浏览器字幕叠加层

适合场景：

- B 站虚拟主播
- 游戏直播陪聊
- 日语内容同传字幕
- AI 主播工作流自动化

---

## 2. 主要功能描述

### 2.1 智能回复链路

- 多 LLM 提供商顺序调用（DeepSeek / GLM / OpenAI）
- 超时与失败自动切换
- 本地问答库兜底（`db.sqlite3`）
- 用户历史上下文保留（可配置条数）

### 2.2 语音播报链路

- 回复文本进入音频队列后异步合成
- 支持并发合成、顺序播放
- 内置队列背压保护，避免弹幕高峰内存暴涨
- 可设置 `mpv` 音量、音频设备与滤镜

### 2.3 欢迎词与心跳

- 支持首次弹幕欢迎词
- 支持进房欢迎事件
- 支持冷场心跳播报（可关闭）
- 带用户缓存淘汰机制，适合长时直播

### 2.4 实时字幕侧车（`live_subtitles.py`）

- 音频捕获：`ffmpeg_source_url` / `ffmpeg_dshow` / `soundcard_loopback`
- ASR：优先 `persistent_faster_whisper`，失败自动回退 `whisper_cli`
- 翻译：异步多 worker，支持 provider 顺序与失败冷却
- 输出文件：双语、原文、译文三份
- Overlay：HTTP API + 浏览器页面给 OBS 使用

### 2.5 工程与稳定性增强（本次优化）

- 新增统一配置读取工具 `settings_utils.py`
- 主播主链路增加音频队列上限与丢弃策略
- 长会话缓存增加 TTL / 上限清理
- TTS 增加 `edge-tts` 超时保护
- Subtitle Overlay 增加按 mtime 缓存，降低磁盘读压
- 新增 `requirements.txt`，补齐依赖

---

## 3. 环境要求

- Windows（推荐）
- Python 3.10+
- `mpv` 可执行文件
- 可选：`ffmpeg`、`faster-whisper` 模型与二进制

安装依赖：

```powershell
pip install -r requirements.txt
```

---

## 4. 快速启动

### 4.1 准备配置

1. 复制模板：

```powershell
copy .\local_settings.example.py .\local_settings.py
```

2. 按机器实际路径修改：

- `SUBTITLE_FFMPEG_PATH`
- `SUBTITLE_WHISPER_PATH`
- `SUBTITLE_WHISPER_MODEL_DIR`

3. 配置 API Key（推荐环境变量）：

- `OPENAI_API_KEY`
- `DEEPSEEK_API_KEY` / `DEEPSEEK_KEY`
- `GLM_API_KEY`
- `VOCU_API_KEY`（如使用 Vocu）

### 4.2 启动主程序

```powershell
python .\main.py
```

输入直播间 ID 后开始监听弹幕。

---

## 5. 字幕系统使用

### 5.1 只跑字幕侧车

```powershell
python -u .\live_subtitles.py
```

### 5.2 启动叠加层服务

```powershell
python -u .\subtitle_overlay_server.py
```

### 5.3 OBS 浏览器源地址

- 双语：`http://127.0.0.1:18082/subtitle_overlay.html?mode=bilingual&count=4&interval=400`
- 译文：`http://127.0.0.1:18082/subtitle_overlay.html?mode=translated&count=4&interval=400`
- 原文：`http://127.0.0.1:18082/subtitle_overlay.html?mode=origin&count=4&interval=400`

---

## 6. 常用参数说明

### 6.1 主播回复链路

- `VTUBER_LLM_ORDER`：模型调用顺序
- `VTUBER_REPLY_DEADLINE`：整条回复链路总超时
- `VTUBER_PROVIDER_TIMEOUT`：单 provider 超时
- `VTUBER_HISTORY_PAIRS`：上下文轮数

### 6.2 音频链路

- `AUDIO_SYNTH_WORKERS`：合成并发
- `AUDIO_QUEUE_MAX_SIZE`：音频队列上限
- `MPV_VOLUME`：播放音量
- `EDGE_TTS_TIMEOUT_SECONDS`：Edge TTS 超时

### 6.3 字幕链路

- `SUBTITLE_CAPTURE_BACKEND`：采集后端
- `SUBTITLE_ASR_BACKEND`：识别后端
- `SUBTITLE_TRANSLATION_WORKERS`：翻译并发
- `SUBTITLE_TRANSLATION_PENDING_LIMIT`：翻译排队上限

---

## 7. 本地问答库训练

将问答写入 `db.txt`（一问一答交替），然后执行：

```powershell
python .\train.py
```

生成的内容写入 `db.sqlite3`，主程序将自动用于兜底回复。

---

## 8. 日志与排障

关键日志：

- `output.txt`：弹幕与回复文本
- `live_subtitles.log` / `live_subtitles_stdout.log`
- `subtitle_overlay.log`

排障建议：

1. 无声音：检查 `mpv.exe` 路径和音频设备
2. 字幕不更新：先看 `live_subtitles` 日志是否持续产出 `[subtitle][chunk]`
3. 翻译慢：提高 `SUBTITLE_TRANSLATION_WORKERS` 并检查 provider API 配额

---

## 9. 商业化建议（可售卖方向）

- **版本分层**：
  - 个人版：基础回复 + TTS
  - 专业版：字幕侧车 + OBS Overlay + 多语言翻译
  - 定制版：专属角色话术、自动化运营脚本
- **交付形态**：
  - 远程部署服务
  - 私有化打包（按机器授权）
  - 技术支持订阅（月度）

---

## 10. 安全与合规建议

- API Key 只放环境变量或 `local_settings.py`（该文件已 gitignore）
- 直播内容遵守平台规范与版权要求
- 商业售卖前建议补齐 EULA、免责声明、售后范围
