# Audio Mixing Guide

当前项目的 AI 语音链路是:

1. TTS 先生成 `mp3`
2. `main.py` 再调用 `mpv.exe` 播放

这意味着:

- `EDGE_TTS_VOLUME` 只能调 Edge TTS 合成出来的音量
- 播放阶段原本没有单独的 AI 语音音量、输出设备、压缩器配置
- 如果视频、音乐、AI 语音都直接混到一个输出里，后面很难再精细调整

现在播放器新增了 3 个配置项:

- `MPV_VOLUME`: 只控制 AI 语音播放音量
- `MPV_AUDIO_DEVICE`: 把 AI 语音路由到指定音频设备
- `MPV_AF`: 给 AI 语音加 `mpv` 音频滤镜

## Recommended Start

把下面几项加到 `local_settings.py` 的 `SETTINGS` 里:

```python
SETTINGS = {
    "MPV_VOLUME": 115,
    "MPV_AUDIO_DEVICE": "",
    "MPV_AF": "lavfi=[acompressor=threshold=-18dB:ratio=4:attack=15:release=180,alimiter=limit=-2dB]",
}
```

起步建议:

- `MPV_VOLUME`: 先试 `110` 到 `125`
- `MPV_AF`: 先用压缩 + 限幅，让 AI 语音更靠前、更稳
- 如果你已经有 OBS / Voicemeeter，再把播放器输出单独路由出去

## Separate Tracks First

最重要的不是先拉大音量，而是先分轨:

- AI 语音一条轨
- 音乐一条轨
- 视频原声一条轨

如果三者先进了同一个设备，再去调 OBS 推流总线，空间会很小。

常见做法:

- AI 语音输出到 `VB-CABLE` 或 Voicemeeter 的某个虚拟输入
- 音乐播放器和浏览器走默认桌面音频
- OBS 里分别采集 AI 语音和桌面音频

查看 `mpv` 可用设备:

```powershell
.\mpv.exe --audio-device=help
```

拿到设备名后，填到:

```python
"MPV_AUDIO_DEVICE": "wasapi/设备名"
```

具体名字以你机器上 `mpv` 输出结果为准。

## OBS Ducking

如果你在直播或录制，推荐把“压背景声”放到 OBS 做:

1. 给 BGM / 视频声音那条轨加 `Compressor`
2. `Sidechain / Ducking Source` 选 AI 语音那条轨
3. 先用下面这组起步值

建议起步:

- Threshold: `-30 dB` 左右
- Ratio: `6:1`
- Attack: `10` 到 `20 ms`
- Release: `150` 到 `250 ms`
- Output Gain: `0`

听感目标:

- AI 一说话，背景声自动降 `8` 到 `12 dB`
- AI 停下后，背景声在 `0.15` 到 `0.25` 秒内回升

如果你放的是“带人声的视频”，通常要比纯 BGM 再多压一点，或者直接在 AI 说话时暂停视频原声。

## Practical Targets

经验上可以按这个方向调:

- AI 语音: 明显站前面，但不刺耳、不爆音
- 纯 BGM: 比 AI 低 `8` 到 `12 dB`
- 视频原声: 比 AI 低 `10` 到 `14 dB`

如果 AI 声音已经足够大但还是“听不清”，一般不是单纯音量问题，而是:

- 中频不够突出
- 背景声没有被 duck
- 峰值太大、平均响度不稳

这时优先保留压缩器和限幅器，不要只靠继续加大 `MPV_VOLUME`。

## Notes

- `Vocu` 当前在这个项目里主要控制的是合成参数，不负责播放侧混音
- `EDGE_TTS_VOLUME` 和 `MPV_VOLUME` 可以同时存在
- 一般先把 TTS 合成音量保持正常，再用 `MPV_VOLUME` 做最终播放平衡，会更好控
