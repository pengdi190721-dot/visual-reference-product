#!/usr/bin/env python3
"""检查证据账本结构与引用；不替代看图，不测量视觉真实性。"""
import json
from pathlib import Path
import sys

DIMENSIONS = {"媒介", "主体", "构图", "留白", "色彩", "光影", "质感", "人物", "场景", "文字", "装饰", "风格"}


def validate(data):
    errors = []

    def check(ok, text):
        if not ok:
            errors.append(text)

    check(data.get("version") == 1, "版本应为 1")
    images = data.get("images", [])
    check(isinstance(images, list) and bool(images), "图片清单不能为空")
    if not isinstance(images, list):
        return errors
    ids = [x.get("id") for x in images]
    check(all(isinstance(x, str) and x for x in ids) and len(set(ids)) == len(ids), "图片编号须非空且唯一")
    image_map = {x.get("id"): x for x in images}
    for item in images:
        key = item.get("id")
        check(item.get("role") in ("style", "product", "person"), f"{key} 图片角色错误")
        check(item.get("status") in ("inspected", "unreadable", "pending"), f"{key} 观察状态错误")
        check(bool(item.get("path")), f"{key} 缺来源路径")
        if item.get("status") != "inspected":
            check(bool(item.get("reason")), f"{key} 未观察须写原因")
        duplicate = item.get("duplicate_of")
        if duplicate:
            check(duplicate in image_map and duplicate != key, f"{key} 重复图指向错误")
            visited = {key}
            while duplicate in image_map:
                if duplicate in visited:
                    errors.append(f"{key} 重复图关系成环")
                    break
                visited.add(duplicate)
                duplicate = image_map[duplicate].get("duplicate_of")
    refs = {x["id"] for x in images if x.get("role") == "style" and x.get("status") == "inspected"}
    all_refs = {x["id"] for x in images if x.get("role") == "style"}
    check(bool(all_refs), "至少应有一张对标图")
    claims = data.get("claims", [])
    claim_ids = [x.get("id") for x in claims]
    check(all(isinstance(x, str) and x for x in claim_ids) and len(set(claim_ids)) == len(claim_ids), "证据编号须非空且唯一")
    claim_map = {x.get("id"): x for x in claims}
    coverage = {key: set() for key in refs}
    for claim in claims:
        key = claim.get("id")
        source = claim.get("image_id")
        check(source in image_map and image_map[source].get("status") == "inspected", f"{key} 不能引用未观察图片")
        check(claim.get("dimension") in DIMENSIONS, f"{key} 未知维度")
        if source in coverage:
            coverage[source].add(claim.get("dimension"))
        kind = claim.get("kind")
        confidence = claim.get("confidence")
        check(kind in ("observation", "inference", "unknown", "not_applicable"), f"{key} 结论类型错误")
        check(confidence in ("高", "中", "低", "未知"), f"{key} 置信度须使用四级制")
        check(bool(claim.get("reason")), f"{key} 缺置信度理由")
        check(bool(claim.get("statement")), f"{key} 缺具体结论或未知说明")
        check(bool(claim.get("evidence")), f"{key} 缺可见依据或无法观察原因")
        bbox = claim.get("bbox")
        if bbox is not None:
            valid = (isinstance(bbox, list) and len(bbox) == 4
                     and all(type(v) in (int, float) and 0 <= v <= 1 for v in bbox))
            check(valid, f"{key} 区域坐标应为 0—1 的四个数字")
            if valid:
                check(bbox[0] < bbox[2] and bbox[1] < bbox[3], f"{key} 区域坐标顺序错误")
            check(claim.get("bbox_method") in ("estimated", "measured"), f"{key} 须注明区域是估计还是测量")
        else:
            check(bool(claim.get("region_note")), f"{key} 无坐标须写定位限制")
        if kind in ("unknown", "not_applicable"):
            check(confidence == "未知", f"{key} 未知/不适用不能写高置信度")
        if kind == "inference":
            check(bool(claim.get("alternatives")), f"{key} 推断须记录替代解释")
    for key, dimensions in coverage.items():
        check(dimensions == DIMENSIONS, f"{key} 缺维度：{'、'.join(sorted(DIMENSIONS - dimensions))}")
    groups = data.get("groups", [])
    group_ids = [g.get("id") for g in groups]
    check(all(isinstance(x, str) and x for x in group_ids) and len(group_ids) == len(set(group_ids)), "分组编号须非空且唯一")
    members = []
    for group in groups:
        gid = group.get("id")
        items = group.get("members", [])
        check(bool(items), f"{gid} 风格组为空")
        check(all(x in refs for x in items), f"{gid} 只能包含已观察的对标图")
        check(group.get("representative") in items, f"{gid} 代表图须属于本组")
        members.extend(items)
        check(bool(group.get("rationale")), f"{gid} 缺归类依据")
        check(bool(group.get("evidence_ids")), f"{gid} 缺共同特征证据")
        for evidence in group.get("evidence_ids", []):
            check(evidence in claim_map and claim_map[evidence].get("image_id") in items, f"{gid} 证据不属于本组")
    check(set(members) == refs and len(members) == len(set(members)), "已观察对标图必须逐张且只归一个主组；离群图可单列一组")
    pending = data.get("unresolved_images", [])
    check(set(pending) == all_refs - refs and len(pending) == len(set(pending)), "未完成对标图清单不匹配")
    for transfer in data.get("transfers", []):
        check(bool(transfer.get("implementation")), "迁移项缺新图实施方式")
        check(bool(transfer.get("evidence_ids")), "迁移项缺来源证据")
        for key in transfer.get("evidence_ids", []):
            claim = claim_map.get(key, {})
            check(claim.get("confidence") in ("高", "中") and claim.get("kind") in ("observation", "inference"), f"迁移项引用低置信度、未知或不存在的证据：{key}")
    for fact in data.get("product_facts", []):
        source = fact.get("source_kind")
        check(source in ("user", "image", "creative", "unknown"), "产品字段来源类型错误")
        check(bool(fact.get("field")), "产品事实缺字段名")
        check(fact.get("category") in ("appearance", "specification", "scene"), "产品字段分类错误")
        if source == "unknown":
            check(fact.get("value") is None and not fact.get("copy_allowed"), "未知参数必须留空且不能进入事实文案")
        else:
            check(bool(fact.get("source_ref")) and fact.get("value") is not None, "已知产品字段缺值或来源")
        if source == "image":
            image = image_map.get(fact.get("source_ref"), {})
            check(image.get("role") == "product" and image.get("status") == "inspected", "图片产品事实只能引用已观察的产品图")
            check(fact.get("category") == "appearance", "图片外观不能证明规格、材料成分或性能")
        if source == "creative":
            check(fact.get("category") == "scene" and not fact.get("locked_fact"), "创意设定不能成为锁定产品事实")
    # 视觉方案中的创意补图独立记录，不升级为 product_facts。
    drafts = data.get("draft_elements", [])
    check(isinstance(drafts, list), "草稿元素应为列表")
    if not isinstance(drafts, list):
        return errors
    draft_ids = set()
    for item in drafts:
        if not isinstance(item, dict):
            errors.append("草稿元素应为对象")
            continue
        key = item.get("id")
        valid_id = isinstance(key, str) and bool(key) and key not in draft_ids
        check(valid_id, "草稿元素编号须非空且唯一")
        if isinstance(key, str):
            draft_ids.add(key)
        origin = item.get("origin")
        check(origin in ("crop", "generated", "suggested_copy"), f"{key} 草稿来源错误")
        check(bool(item.get("location")) and bool(item.get("content")), f"{key} 缺成图位置或内容")
        status = item.get("confirmation_status")
        check(status in ("pending", "confirmed", "rejected"), f"{key} 确认状态错误")
        if status == "confirmed":
            check(bool(item.get("confirmation_ref")), f"{key} 确认必须有用户核对依据")
        if origin == "crop":
            source = image_map.get(item.get("source_ref"), {})
            check(source.get("role") == "product" and source.get("status") == "inspected", f"{key} 裁切须引用已观察产品图")
            box = item.get("bbox")
            check(isinstance(box, list) and len(box) == 4
                  and all(type(v) in (int, float) and 0 <= v <= 1 for v in box)
                  and box[0] < box[2] and box[1] < box[3], f"{key} 缺有效裁切区域")
        elif origin in ("generated", "suggested_copy"):
            check(bool(item.get("invented_or_unverified")), f"{key} 须明确创意补充/未核实的内容")
            if status != "confirmed":
                check(item.get("draft_only") is True, f"{key} 未确认创意只能作为草稿")
        if status == "rejected":
            check(item.get("included_in_current") is False, f"{key} 已否定内容不能继续用于当前交付")
    delivery = data.get("delivery_status", "draft")
    check(delivery in ("draft", "ready"), "交付状态应为 draft 或 ready")
    if delivery == "ready":
        check(all(x.get("confirmation_status") == "confirmed"
                  or x.get("included_in_current") is False for x in drafts if isinstance(x, dict)),
              "仍有使用中的未确认细节/文案，不能标为事实已确认交付")
    return errors


def main():
    if len(sys.argv) != 2:
        print("用法：python3 scripts/check_analysis.py 证据账本.json", file=sys.stderr)
        return 2
    try:
        data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8-sig"))
        errors = validate(data)
    except (OSError, ValueError, TypeError, AttributeError, KeyError) as exc:
        print("结构错误：" + str(exc), file=sys.stderr)
        return 2
    complete = not errors and not data.get("unresolved_images")
    print(json.dumps({"valid": not errors, "analysis_complete": complete, "errors": errors, "limit": "仅检查结构与引用；未证明视觉解析准确性。"}, ensure_ascii=False, indent=2))
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
