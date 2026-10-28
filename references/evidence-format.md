# 机器可检查的证据账本

人类可读的主交付使用 `templates/analysis.md`。需要检查逐图覆盖、分组和来源引用时，再按 `templates/evidence.json` 维护同一份内容的结构化数据；不要让两版结论互相矛盾。

## 字段

- `images`：每个输入一条，包含 `id`、`role`（`style/product/person`）、`path`、`status`（已观察 `inspected` / 无法读取 `unreadable` / 待处理 `pending`）；未观察须有 `reason`。`duplicate_of` 记录重复图主编号，没有则 `null`。
- `claims`：每条证据记录 `id`、`image_id`、`dimension`、`statement`、`evidence`、`kind`、`confidence`、`reason`、`bbox`、`bbox_method`。区域为空时补 `region_note`。
- `kind`：观察 `observation`、推断 `inference`、未知 `unknown`、不适用 `not_applicable`。推断另填 `alternatives`；未知与不适用的置信度统一为“未知”。
- `dimension`：规范中的十二个中文维度。已观察的每张对标图必须全部覆盖；缺内容写不适用而不是省略。
- `groups`：每组包含 `id`、`members`、`representative`、`rationale`、`evidence_ids`。已观察对标图每张有且仅有一个主分组。待判断图放 `unresolved_images`。
- `transfers`：每条含 `evidence_ids` 和 `implementation`，并建议补 `conflict` 与 `decision`。低置信度和未知证据不能直接迁移成确定规则。
- `product_facts`：`field`、`value`、`source_kind`（`user/image/creative/unknown`）、`source_ref`、`category`（外观 `appearance` / 规格性能 `specification` / 场景 `scene`）、`copy_allowed`、`locked_fact`。未知值用 `null`；图片来源只能证明可见外观；这张事实表中的创意只用于场景；创意产品补图/文案放入独立的 `draft_elements`，不得充作事实。

## 单条例子（仅演示写法，不是对任何真实图片的结论）

```json
{
  "id": "E-R01-01",
  "image_id": "R01",
  "dimension": "构图",
  "statement": "主体位于右侧",
  "evidence": "主体完整外轮廓出现在右侧，左侧保留大块背景",
  "kind": "observation",
  "confidence": "高",
  "reason": "主体与背景边缘清晰，位置不受遮挡影响",
  "bbox": [0.52, 0.18, 0.96, 0.92],
  "bbox_method": "estimated"
}
```

```sh
python3 scripts/check_analysis.py /任务目录/evidence.json
```

`valid` 仅表示结构与引用一致；`analysis_complete` 才表示账本中没有待观察对标图。两者都不能证明视觉判断准确。模板初始状态允许保存“待处理”，不把它显示为“解析已完成”。

## 视觉草稿元素

`draft_elements` 为可选列表，`delivery_status` 为 `draft`（默认）或 `ready`（所有使用中的详情/文案已确认）。每项填写：

- `id`：唯一编号；`origin`：原图裁切 `crop`、创意补图 `generated`、拟用文案 `suggested_copy`。
- `location`、`content`：实际成图位置与内容，便于用户逐项核对。
- 裁切填 `source_ref`（已观察产品图）、`bbox`（原图归一化区域）。
- 创意补图/文案填 `invented_or_unverified`，明确缺乏依据的具体部分。
- `confirmation_status`：`pending/confirmed/rejected`；确认填 `confirmation_ref`；生成详情/拟用文案必须是用户核对原话或消息依据，真实裁切可引用已经观察的原图及对应区域，不重复盘问学员。
- 未确认创意填 `draft_only: true`；被用户否定的内容须从当前交付排除，并填 `included_in_current: false`，等待替换。

用户确认后仍保留生成来源，不将 AI 设想重新标为实拍。`ready` 只是记录中的用户核对状态，不等于客观检测结果或视觉验收已通过。
