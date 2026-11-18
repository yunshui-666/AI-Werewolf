# 界面素材与语音接口

## 卡通角色素材

`web/static/assets/characters.png` 通过内置 ImageGen 生成，是一张六角色图集。顺序为第一行狼人、预言家、女巫，第二行猎人、守卫、村民。前端使用 CSS 背景位置展示相应角色。

桌面项目的图集保存路径为 `C:\Users\Mark\Desktop\WerewolfArena\web\static\assets\characters.png`。

进行中的未知身份统一使用中性村民外观，变化只来自座位编号对应的饱和度。自己的角色和已公开身份使用角色形象，终局统一展示全部底牌。

生成提示词如下。

```text
Use case: stylized-concept. Asset type: a single production sprite atlas for a local werewolf social-deduction game's character cards. Create ONE square illustration atlas with a perfectly regular 3 columns by 2 rows grid, six equal square cells, crisp aligned cell boundaries. Each cell is a front-facing waist-up cartoon portrait at identical scale, centered within its cell with 12% safe margins. Consistent polished hand-painted 3D storybook style, sophisticated cute characters, rich midnight navy, warm parchment and muted brass accents, soft cinematic rim light, detailed expressive faces. Reading order top-left to bottom-right: (1) a clever gray werewolf in a dark cloak, charming not scary; (2) a calm female seer holding a softly glowing crystal orb, deep blue robes; (3) a young witch with a pointed plum hat and two small potion bottles; (4) a friendly rugged hunter in a forest-green coat with a slung medieval crossbow; (5) a steadfast female guardian with a round shield and subtle steel armor; (6) a friendly ordinary villager with auburn hair, linen shirt and brown waistcoat, no magical props. Background in each cell is flat midnight navy #172033 with a subtle circular glow, matching all six portraits. Face and main silhouette must be centered. No text, no labels, no numbers, no logos, no grid strokes. These are original cartoon game characters for a Chinese werewolf game. No blood or gore. Output exact square composition with the regular 3x2 atlas filling the canvas.
```

实际图集为正方形画布上的 3×2 肖像网格，单格是纵向肖像，CSS 按网格位置展示。

## 圆桌布局参考

界面借鉴狼人杀语音房常见的编号座位、当前发言者、按顺序发言与公共记录组织形式。牌桌和卡片布局由本项目实现。 [在线对局流程参考](https://hi8.com/g/langrensha) [语音房流程示例](https://main.qcloudimg.com/raw/document/debug/product/pdf/647_41102_cn.pdf)

## 语音扩展

当前 `BrowserTTSAdapter` 使用浏览器 Web Speech API。每个座位可保存不同 `voiceURI`，具体声线由本机提供，`SpeechQueue` 保证一条发言结束后再播放下一条。

自定义声色服务可实现同样的适配器协议。

```javascript
class CustomVoiceAdapter {
  voices() {
    // 返回你后续服务提供的可用声线列表。
    return [];
  }
  async speak(text, options) {
    // options.voiceURI 对应用户为该座位选择的声线。
    // 请求你自己的 TTS 后端，播放返回的音频，并在播放完成时 resolve。
  }
  cancel() {
    // 停止当前音频并释放播放资源。
  }
}
```

将实例传入 `SpeechQueue` 即可复用逐座位偏好与队列逻辑。自定义服务的鉴权和音频生成仍应由 Python 后端持有，接口地址及 Key 可在后续实现时配置。
