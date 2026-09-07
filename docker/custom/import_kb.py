"""Import Xinglan test markdown into Dify knowledge bases.

Run inside the api container:
  python /tmp/import_kb.py
"""

from __future__ import annotations

import os
from pathlib import Path

from flask import g
from sqlalchemy import select

from app_factory import create_app
from core.rag.retrieval.retrieval_methods import RetrievalMethod
from extensions.ext_database import db
from models import Account, Tenant, TenantAccountJoin
from models.dataset import Dataset, DatasetPermissionEnum, Document
from models.enums import ProcessRuleMode
from services.dataset_service import DatasetService, DocumentService
from services.entities.knowledge_entities.knowledge_entities import KnowledgeConfig, RetrievalModel
from services.file_service import FileService

ROOT = Path(os.environ.get("KB_IMPORT_ROOT", "/tmp/knowledge-bases"))

DATASETS = [
    {
        "name": "星澜-OA内部流程",
        "description": "星澜智造 OA 审批、考勤、报销、合同等内部流程（测试语料）",
        "folder": "01-OA内部流程",
        "max_tokens": 700,
    },
    {
        "name": "星澜-公司客户资料",
        "description": "星澜智造客户主数据、KA 档案与回款资料（测试语料）",
        "folder": "02-公司客户资料",
        "max_tokens": 600,
    },
    {
        "name": "星澜-公司规章制度",
        "description": "星澜智造员工手册、信息安全、财务纪律等制度（测试语料）",
        "folder": "03-公司规章制度",
        "max_tokens": 500,
    },
]


def _skip_file(path: Path) -> bool:
    if path.suffix.lower() not in {".md", ".markdown"}:
        return True
    if path.name.lower() == "readme.md":
        return True
    if path.name.startswith("00-"):
        return True
    return False


def _process_rule(max_tokens: int) -> dict:
    return {
        "mode": ProcessRuleMode.CUSTOM,
        "rules": {
            "pre_processing_rules": [
                {"id": "remove_extra_spaces", "enabled": True},
                {"id": "remove_urls_emails", "enabled": False},
            ],
            "segmentation": {
                "separator": "\n## ",
                "max_tokens": max_tokens,
                "chunk_overlap": 50,
            },
        },
    }


def _retrieval_model() -> RetrievalModel:
    return RetrievalModel(
        search_method=RetrievalMethod.KEYWORD_SEARCH,
        reranking_enable=False,
        top_k=5,
        score_threshold_enabled=False,
    )


def main() -> None:
    if not ROOT.is_dir():
        raise SystemExit(f"knowledge-bases not found: {ROOT}")

    _, flask_app = create_app()
    with flask_app.app_context(), flask_app.test_request_context():
        session = db.session
        join = session.scalar(select(TenantAccountJoin).limit(1))
        if join is None:
            raise SystemExit("No workspace account found. Open Dify and finish setup first.")
        account = session.get(Account, join.account_id)
        tenant = session.get(Tenant, join.tenant_id)
        if account is None or tenant is None:
            raise SystemExit("Workspace account or tenant is missing.")
        account.set_current_tenant_with_session(tenant, session=session)
        g._login_user = account

        file_service = FileService(db.engine)
        created_docs = 0
        skipped_docs = 0

        for spec in DATASETS:
            dataset = session.scalar(
                select(Dataset).where(Dataset.tenant_id == tenant.id, Dataset.name == spec["name"]).limit(1)
            )
            if dataset is not None:
                DatasetService.delete_dataset(dataset.id, account, session)
                print(f"removed previous dataset: {spec['name']}")

            dataset = DatasetService.create_empty_dataset(
                tenant_id=tenant.id,
                name=spec["name"],
                description=spec["description"],
                indexing_technique="economy",
                account=account,
                permission=DatasetPermissionEnum.ALL_TEAM,
                retrieval_model=_retrieval_model(),
                session=session,
            )
            print(f"created dataset: {spec['name']}")

            folder = ROOT / spec["folder"]
            files = sorted(p for p in folder.glob("*.md") if not _skip_file(p))
            existing_names = set(
                session.scalars(select(Document.name).where(Document.dataset_id == dataset.id)).all()
            )

            for path in files:
                if path.name in existing_names or path.stem in existing_names:
                    print(f"  skip exists: {path.name}")
                    skipped_docs += 1
                    continue
                text = path.read_text(encoding="utf-8")
                upload_file = file_service.upload_text(
                    text=text,
                    text_name=path.name,
                    user_id=account.id,
                    tenant_id=tenant.id,
                )
                knowledge_config = KnowledgeConfig.model_validate(
                    {
                        "name": path.name,
                        "indexing_technique": "economy",
                        "doc_form": "text_model",
                        "doc_language": "Chinese",
                        "process_rule": _process_rule(spec["max_tokens"]),
                        "data_source": {
                            "info_list": {
                                "data_source_type": "upload_file",
                                "file_info_list": {"file_ids": [upload_file.id]},
                            }
                        },
                    }
                )
                DocumentService.document_create_args_validate(knowledge_config)
                documents, batch = DocumentService.save_document_with_dataset_id(
                    dataset=dataset,
                    knowledge_config=knowledge_config,
                    account=account,
                    created_from="api",
                    session=session,
                )
                print(f"  queued {path.name} -> {documents[0].id} batch={batch}")
                created_docs += 1

        print(f"done. new_docs={created_docs} skipped={skipped_docs}")


if __name__ == "__main__":
    main()
