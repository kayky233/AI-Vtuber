# AI Vtuber

## License

This repository is now proprietary (`All Rights Reserved`). See `LICENSE` and `CLOSED_SOURCE_NOTICE.md`.

> 商用使用建议先阅读：`USER_MANUAL.md`
>
> 依赖安装（推荐）：
>
> ```bash
> pip install -r requirements.txt
> ```

AI Vtuber是一个简单的虚拟主播，可以在Bilibili直播中与观众实时互动。它支持 OpenAI、DeepSeek、GLM 三家兼容 `chat/completions` 的接口，也支持 Edge TTS / Vocu 两种语音合成方式，并保留本地问答语料作为兜底。

交流群：[745682833](https://jq.qq.com/?_wv=1027&k=IO1usMMj)

### 运行环境
- Python 3.6+
- Windows操作系统

### 安装依赖
在命令行中使用以下命令安装所需库：
```bash
pip install bilibili-api-python edge-tts httpx
```
此外，还需要[下载并安装mpv](https://mpv.io/installation/)。在Windows操作系统上，也需要将 `mpv.exe` 添加到环境变量中。对于其他操作系统，请将其路径添加到系统 `PATH` 环境变量中。

项目当前不再依赖 ChatterBot、spaCy 或 PyTorch，因此不需要额外处理这些大型依赖。

### 配置
1. 可选环境变量：
   - `OPENAI_API_KEY`
   - `DEEPSEEK_API_KEY` 或 `DEEPSEEK_KEY`
   - `GLM_API_KEY`
   - `OPENAI_MODEL`、`DEEPSEEK_MODEL`、`GLM_MODEL`
   - `VTUBER_LLM_ORDER`，例如 `deepseek,glm,openai`
   - `local_settings.py` 里的本地默认值会在没有环境变量时自动生效，环境变量优先级更高
   - 可复制的模板见 `local_settings.example.py`
2. 如果三家接口都失败，程序会自动回退到本地 `db.sqlite3` 语料库。
   常用加速参数：
   - `VTUBER_REPLY_DEADLINE`：整条回复链路的总时限，默认 `6`
   - `VTUBER_PROVIDER_TIMEOUT`：单个模型的等待上限，默认 `4`
   - `VTUBER_LLM_MAX_TOKENS`：回复长度上限，默认 `80`
   - `VTUBER_HISTORY_PAIRS`：带给模型的历史轮数，默认 `2`
   - `VTUBER_LOCAL_FIRST_SCORE`：高相似度本地语料直接秒回的阈值，默认 `0.92`
3. 语音配置：
   - 默认是 Edge TTS
   - `TTS_PROVIDER=edge`
   - `EDGE_TTS_VOICE`、`EDGE_TTS_RATE`、`EDGE_TTS_PITCH`
4. Vocu 配置：
   - `TTS_PROVIDER=vocu`
   - `VOCU_API_KEY`
   - `VOCU_VOICE_ID`，或者 `VOCU_SHARE_ID`
   - `VOCU_PROMPT_ID`，默认 `default`
   - `VOCU_PRESET`，默认 `v2_balance`

使用 Vocu 市场声音时，推荐流程：
1. 在市场页先点击“添加到角色库”。
2. 去 Vocu 开发者后台申请 API Key。
3. 在你自己的角色库中找到这个角色的 `voiceId`，填到 `VOCU_VOICE_ID`。如果 Vocu 页面给你的是 `market:角色ID`，也可以直接填，程序会自动走异步生成接口。
4. 如果你拿到的是分享链接或分享 ID，也可以直接填 `VOCU_SHARE_ID`，程序会在首次调用时尝试导入。

### 使用
1. 在命令行中运行以下命令启动程序：
```bash
python main.py
```
2. 输入要连接的B站直播间编号。
3. 按下`Enter`键开始监听弹幕流。

当有观众发送弹幕消息时，机器人将自动生成回复并将其转换为语音。声音文件将被保存并立即播放。

启动时会打印当前回复链路，例如：
```text
回复链路：deepseek(deepseek-chat) -> glm(glm-4.7-flash) -> openai(gpt-4o-mini) -> local-db
```

### 如何训练自己的AI？
- 打开`db.txt`，写入你想要训练的内容，格式如下
```
问
答
问
答
```
- 在命令行中运行以下命令启动程序：
```bash
python train.py
```
- 训练结果会写入`db.sqlite3`，然后运行`main.py`即可使用
- 没有语料？快来加群下载吧！[745682833](https://jq.qq.com/?_wv=1027&k=IO1usMMj)

### 常见问题
1. 运行 `train.py` 提示 `db.txt 还是模板内容`
```text
把 db.txt 替换成你自己的问答语料，再重新运行 python train.py
```
2. 运行 `main.py` 没有声音
```text
确认已经安装 edge-tts，并且 mpv.exe 可以在项目目录下直接运行
```

### TODO
- [ ] 优化问答匹配策略
- [ ] 支持更复杂的语料格式

### 许可证
MIT许可证。详情请参阅LICENSE文件。
