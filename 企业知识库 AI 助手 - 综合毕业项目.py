"""
企业知识库 AI 助手 —— 综合毕业项目（工程化升级版 v2.0）

快速启动（与原版一致）：
    python 企业知识库 AI 助手 - 综合毕业项目.py

依赖安装：
    pip install -r requirements.txt

核心能力：
- Agentic RAG：LangGraph ReAct 代理 + 多路召回 + RRF 融合 + 重排序
- 持久化：文档/分块/向量落 SQLite，重启自动恢复，不再丢数据
- 工程化：模块化分层（app/ 包）、配置集中管理、健康检查、单元测试
- 部署：Docker / docker-compose 一键启动

入口仅做「引导」：加载 app.main 并启动 uvicorn。
原版单文件已备份为：企业知识库 AI 助手 - 综合毕业项目_原版备份.py
"""
import logging
import sys

import uvicorn

from app.config import settings

logger = logging.getLogger("entry")


def main() -> None:
    settings.validate()
    # 延迟导入：让 uvicorn 直接加载应用（保证 reload 场景行为一致）
    from app.main import app

    logger.info("启动服务：http://%s:%s", settings.HOST, settings.PORT)
    uvicorn.run(app, host=settings.HOST, port=settings.PORT)


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as exc:
        print(f"[启动失败] {exc}", file=sys.stderr)
        sys.exit(1)