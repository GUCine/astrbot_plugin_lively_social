# 好友與群邀請管家

> 好友申請、群邀請、禁言踢出通知，熟人自動通過，其餘私信主人決定

AstrBot 插件，屬於「擬真群友」（lively）插件套件。適用 AstrBot >=4.29.0 + NapCat（aiocqhttp）。

主人、群、機器人名字、人設用詞這些共用設定在 [astrbot_plugin_lively_gate](https://github.com/GUCine/astrbot_plugin_lively_gate) 裡填，建議一起裝。

## 功能

```text
機器人的社交管家：好友申請、群邀請、被禁言/踢出/設管理的通知。

- 聊過天的熟人加好友：自動通過（每天有上限），通過後打個招呼
- 其他好友申請、所有群邀請：私信主人，主人回「同意 / 拒絕 / 拉黑 [QQ或群號]」
- 被禁言、被踢、被拉進新群、管理員變動：私信主人；禁言期間那個群的消息不再交給模型（反正發不出去）
- 所有事件記進 events.jsonl，給每日彙報用
```

## 依賴

可選 astrbot_plugin_lively_bond（用聊天次數判斷是不是熟人）。

## 配置

在 AstrBot WebUI 的插件配置裡填，或者直接編輯 `data/config/astrbot_plugin_lively_social_config.json`。留空的公共項（主人、群、機器人名字、平台）會用 astrbot_plugin_lively_gate 裡填的。

| 配置項 | 說明 | 預設 |
| --- | --- | --- |
| `groups` | 服務的群（留空用門禁插件裡填的） | （空） |
| `owner_qq` | 主人 QQ（留空用門禁插件裡填的） | （空） |

## 資料

執行資料在 `data/plugin_data/astrbot_plugin_lively_social/`。

## 許可

MIT，見 [LICENSE](LICENSE)。
