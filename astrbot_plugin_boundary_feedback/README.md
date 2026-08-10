# 边界与情感反馈（astrbot_plugin_boundary_feedback）

为 [AstrBot](https://github.com/Soulter/AstrBot) 的 [private_companion](https://github.com/) 角色补充**边界与情感反馈闭环**：让角色对越界者不再一味迎合，而是像真人一样——会反感、会扣好感、会冷淡、会生气、会跟朋友吐槽、会向主人告状。

## 功能

- **越界判断（LLM）**：按"当前关系档位"判断用户行为是否越界（对初识的人说情话 vs 对亲近的人撒娇，定性不同），不是查关键词
- **弹性扣好感**：越界扣 `relationship_score`，扣分后按「每 N 秒恢复 1 点」缓慢恢复（扣得越多回得越久）；恢复期内再越界则上一条不恢复（叠加惩罚）
- **阶段反应（计时制）**：按累计扣分推进 回避 → 明令禁止 → 反思（冷落），并写入情绪门状态让角色自然变冷淡
- **道歉机制**：道歉恢复部分好感 + 加速恢复，恢复部分打信任标记写入记忆；再犯同类直接追回；同类道歉过多不再接受
- **底线系统（三级）**：触发角色底线（配置的底线基线，默认通用版）→ 第一次扣大分+警告 / 第二次扣至冷落 / 第三次关系降档
- **跟朋友吐槽 / 向主人告状**（概率性）：越界后角色可能跟亲近的人吐槽（写入生活叙事 `daily_story_plan`），也可能委屈地跟主人说——是"社交行为"，不是每次越界都汇报

## 安装

1. 将 `astrbot_plugin_boundary_feedback` 目录放入 AstrBot 的 `data/plugins/` 下
2. 在 AstrBot 插件管理里启用
3. 配置 `judge_api_key`（越界/底线 LLM 判断用的 API Key；支持 OpenAI 兼容接口）

## 配置项

| 配置 | 说明 | 默认 |
| --- | --- | --- |
| `enabled` | 总开关 | `true` |
| `enable_deduct` | 越界扣好感 | `true` |
| `enable_stage` | 阶段反应（回避/禁止/反思） | `true` |
| `enable_apology` | 道歉恢复与信任标记 | `true` |
| `enable_bottom_line` | 底线系统（三级惩罚） | `true` |
| `enable_vent` | 跟亲近的人吐槽（写生活叙事） | `true` |
| `enable_notify` | 向主人告状 | `true` |
| `deduct_light/mid/severe` | 轻/中/严重越界扣分 | `-2/-5/-8` |
| `recover_seconds_per_point` | 每恢复 1 点好感所需秒数 | `1800` |
| `recover_ratio_light/mid/severe` | 各严重程度可恢复比例 | `0.5/0.33/0.25` |
| `stage_avoid/forbid/reflect_deduct` | 阶段阈值（累计扣分） | `-6/-12/-20` |
| `apology_restore_ratio` | 道歉恢复比例 | `0.6` |
| `apology_speedup_multiplier` | 道歉后恢复加速倍数 | `3.0` |
| `apology_duplicate_limit` | 同类道歉次数上限 | `3` |
| `tattle_probability_*` | 各严重程度告状概率 | `0.85/0.55/0.3/0.12` |
| `vent_probability_*` | 各严重程度吐槽概率 | `0.9/0.6/0.35/0.15` |
| `cold_shoulder_minutes` | 冷落期时长（分钟） | `180` |
| `bottom_line_baseline` | 角色底线基线（LLM 判断用，可写角色专属版） | 通用版 |
| `judge_api_key/base/model` | LLM 判断接口配置 | opencode flash |
| `vent_targets` | 吐槽对象（角色亲近的人） | `["朋友"]` |
| `vent_scene_template` | 吐槽场景模板（`{target}/{who}/{level_desc}/{excerpt}`） | 默认模板 |
| `target_user_ids` | 生效用户列表（空=所有非主人） | `[]` |
| `owner_user_ids` | 主人 ID（越界检测跳过，告状发给这些人） | `[]` |
| `companion_data_path` | companions.json 路径（留空自动探测） | `""` |
| `backup_before_write` | 写数据前备份 | `true` |

## 原理

- 越界/底线判断走 LLM（OpenAI 兼容接口），输入当前关系档位 + 用户消息 + 底线基线
- 扣分/恢复/阶段/冷落直接操作 private_companion 的 `companions.json`（写前备份、原子写、防并发）
- 吐槽写入 `daily_story_plan.today_events`（角色的生活叙事，日程/主动消息/回忆会自然带出）
- 所有概率事件可用配置调节，角色可关闭任意子功能

## 测试

```bash
python -X utf8 tests/test_boundary_feedback.py
```

## License

MIT
