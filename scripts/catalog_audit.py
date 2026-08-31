"""分类与发布检查：默认只读。与网站后台共用预览和确认逻辑。"""
from __future__ import annotations

import argparse
import json

from app.services.category_governance import catalog_audit, merge_categories, merge_preview


def main():
    parser = argparse.ArgumentParser(description="默认只读检查，不修改分类或发布状态")
    parser.add_argument("--source-id", type=int, help="预览需要合并的旧分类编号")
    parser.add_argument("--target-id", type=int, help="预览目标分类编号")
    parser.add_argument("--apply", metavar="FINGERPRINT", help="使用本次预览的fingerprint明确确认合并")
    args = parser.parse_args()
    if bool(args.source_id) != bool(args.target_id) or (args.apply and not args.source_id):
        parser.error("合并预览须同时提供source-id和target-id；apply不能单独使用")
    from app.core.database import SessionLocal
    with SessionLocal() as db:
        try:
            if args.apply:
                log = merge_categories(db, args.source_id, args.target_id, args.apply)
                db.commit()
                result = {"changed": True, "audit_id": log.id, "books": len(log.detail["rows"])}
            elif args.source_id:
                result = {"changed": False, "preview": merge_preview(db, args.source_id, args.target_id)}
            else:
                result = {"changed": False, **catalog_audit(db)}
            print(json.dumps(result, ensure_ascii=False, indent=2))
        except ValueError as exc:
            db.rollback()
            parser.exit(1, str(exc) + "\n")


if __name__ == "__main__":
    main()
