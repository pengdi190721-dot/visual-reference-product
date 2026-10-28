# 机器可检查的证据账本

人类可读的主交付使用 `templates/analysis.md`。需要检查逐图覆盖、分组和来源引用时，再按 `templates/evidence.json` 维护同一份内容的结构化数据；不要让两版结论互相矛盾。

## 字段

- `images`：每个输入一条，包含 `id`、`role`（`style/product/person`）、`path`、`status`（已观察 `inspected` / 无法读取 `unreadable` / 待处理 `pending`）；未观察须有 `reason`。`duplicate_of` 记录重复图主编号，没有则 `null`。
- `claims`：每条证据记录 `id`、`image_id`、`dimension`、`statement`、`evidence`、`kind`、`confidence`、`reason`、`bbox`、`bbox_method`。区域为空时补 `region_note`。
- `kind`：观察 `observation`、推断 `inference`、未知 `unknown`、不适用 `not_applicable`。推断另填 `alternatives`；未知与不适用的置信度统一为“未知”。
- `dimension`：规范中的十二个中文维度。已观察的每张对标图必须全部覆盖；缺内容写不适用而不是省略。
- `groups`：每组包含 `id`、`members`、`representative`、`rationale`、`evidence_ids`。已观察对标图每张有且仅有一个主分组。待判断图放 `unresolved_images`。
- `transfers`：每条含 `evidence_ids` 和 `implementation`，并建议补 `conflict` 与 `decision`。低置信度和未知证据不能直接迁移成确定规则。
- `product_facts`：`field`、`value`、`source_kind`（`user/image/creative/unknown`）、`source_ref`、`category`（外观 `appearance` / 规格性能 `specification` / 场景 `scene`）、`copy_allowed`、`locked_fact`。未知值用 `null`；图片来源只能证明可见外观；创意只用于场景。

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
