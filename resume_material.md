# 简历素材：企业知识库 AI 助手（Agentic RAG）

> 本文档把项目的「简历话术」整理成可直接粘贴的素材。
> 标注 `【量化】` 的指标全部来自真实评测运行（`docs/eval_report.md`，可一键复现）。
> 建议按目标岗位（算法/LLM 应用/NLP/后端）裁剪使用。

---

## 一、一句话定位

基于 **LangGraph Agent + 混合检索 RAG** 的企业知识库智能问答系统：文档上传即问答、多轮对话、流式输出、重启不丢数据，并自带检索质量评测体系。

## 二、项目亮点（bullet 版，可直接放进简历）

- 设计并实现 **Agentic RAG** 知识库问答系统：LangGraph ReAct 代理编排「知识检索 / 计算器 / 转人工」多工具自主决策，支持 SSE 流式作答。
- 自研 **混合检索管线**：FAISS 稠密向量 + BM25（jieba 中文分词）双路召回 → **RRF 无参融合** → bge-reranker Cross-Encoder 精排（Top-20→Top-5），并实现 **LLM 查询改写（RAG-Fusion）** 提升口语化问题召回。
- 【量化】构建 24 题 × 9 篇企业语料评测集，多策略对比评测（真实 API 运行，脚本一键复现）：
  生产全流程 **Hit@3 达 100% / MRR@5 达 97.9% / Top-1 达 95.8%**，
  较纯向量检索基线 **MRR@5 +7.3pp、Top-1 +12.5pp、Hit@3 +4.2pp**（完整报告 `docs/eval_report.md`）。
- 【工程化】FastAPI + Pydantic v2 + SQLite（WAL）持久化，**服务重启自动恢复全部文档与索引**；配置中心集中管理、**零硬编码密钥**、fail-fast 校验。
- 【质量】30 项单元测试覆盖分块/存储/检索/API（全离线）、Docker 一键部署（含健康检查与数据卷）。
- 【多格式解析】插件化解析子系统支持 19 种扩展名（Word/Excel/PPT 含备注/PDF/扫描件 OCR/图片/CSV/HTML/JSON）；
  扫描件判定 + PyMuPDF 渲染 + RapidOCR 离线识别，大文件异步任务化；结构感知分块（标题层级 + 表格语义化 + 章节路径前缀）
  使 **Hit@5 +4.2pp / MRR@5 +7.3pp**，引用可精确到章节。
- 【权限与安全】JWT 认证 + RBAC 四角色 + 文档级 ACL（用户/角色/部门三维授权）+ 密级体系（public/internal/confidential/secret）
  + append-only 审计日志；把**权限过滤下沉到向量与 BM25 的召回阶段**（宽召回 + 白名单过滤），
  从机制上杜绝「先检索后过滤」导致的越权泄露（实测无权限用户提问机密内容零泄露，授权后同题正确作答）。
- 【会话记忆】三层记忆架构：最近 K 轮工作记忆（上下文 token 与轮数呈常量关系）+ 滚动摘要（超阈值 LLM 压缩，
  `summarized_upto` 游标增量合并、后台线程执行不阻塞回答）+ 会话管理（列表/重命名/删除/TTL/用户隔离）；
  服务重启后多轮上下文完整恢复（实测重启后同会话追问，模型准确复述前两轮内容）。
- 【质量门禁】GitHub Actions 三道门：114 项离线单测 / 硬编码密钥扫描 / **检索指标基线回归阻断**
  （逐策略×逐指标对比 Hit@3、Hit@5、MRR@5、Top-1、文档命中，回归超容忍度即失败，评测集 md5 变更默认失效）；
  本地一键复现与更新基线，让「效果不回归」可自动验证。
- 【质量评测】LLM-as-Judge 端到端评测：对「检索+生成」完整链路按**正确性/忠实度/相关性**打分，
  24 题达标率 100%、幻觉率 0%；另设 5 题对抗样本（问知识库未收录的信息）验证拒答能力，
  **正确拒答率 100%**（检索返回无关资料时仍不编造）。
- 【性能优化】语义缓存：以「问题向量相似度 + 权限范围指纹 + 索引版本」为缓存键，
  命中即跳过改写/双路召回/重排，实测 **2345ms → 144ms（16.2×）**；
  通过权限指纹隔离与文档变更版本失效，从机制上避免越权命中与过期结果。

## 三、负责模块 / 技术难点与解法（面试问答素材）

| 难点 | 我的解法 |
| --- | --- |
| 单路检索召回不全（语义 vs 关键词不互补） | 双路召回 + RRF 融合，规避分数尺度不对齐问题 |
| 口语化/长尾问题检索差 | LLM 多角度查询改写（RAG-Fusion），原始问题保底 |
| 大语料 Top-k 噪声 | Cross-Encoder 精排 + 只取 Top-5，可降级容错 |
| 中文分词 | jieba 词典 + BM25Okapi 稀疏索引 |
| 重启丢数据（毕设通病） | SQLite 落盘（文档/分块/向量 BLOB），启动自动恢复并重建索引 |
| 依赖弃用/安全 | 从 langchain-community FAISS 迁到原生 faiss；`create_react_agent` 迁移到 `langchain.agents.create_agent`；去除代码内硬编码 Key |
| 可复现性 | 一键评测脚本 + 自动化报告，指标可随时复现 |

## 四、量化成果（来自真实评测，随时可复现）

**评测设置**：24 道题 × 9 篇企业语料（制度 / 产品 / 运维 / 人事 / 信息安全 / 项目管理 / BI，共 19 个索引分块）；
五种策略对比：纯向量 / 纯 BM25 / 混合 RRF / +查询改写 / 生产全流程（改写+混合+RRF+重排）。

| 策略 | Hit@3 | MRR@5 | Top-1 | 文档命中@5 |
| --- | ---: | ---: | ---: | ---: |
| 纯向量 | 95.8% | 90.6% | 83.3% | 100% |
| 纯 BM25 | 91.7% | 87.5% | 83.3% | 95.8% |
| 混合 RRF | 91.7% | 84.2% | 75.0% | 100% |
| +查询改写 | 100.0% | 93.1% | 87.5% | 100% |
| **全流程** | **100.0%** | **97.9%** | **95.8%** | **100%** |

**一句话量化**：查询改写将 Hit@3 拉满至 100%，重排将 MRR@5 提升至 97.9%（较纯向量 +7.3pp）；
生产管线相对纯向量基线 Top-1 提升 12.5 个百分点。
可复现命令：`python scripts/eval_rag.py`（真实 API 调用，约 3 分钟）。

## 五、技术栈关键词（JD 匹配用）

`RAG` `Agent` `LangGraph` `LangChain` `FastAPI` `FAISS` `BM25` `RRF` `Rerank` `查询改写` `向量检索` `SQLite` `pytest` `Docker` `SSE` `OpenAI 兼容接口` `大模型应用`

## 六、面试高频追问准备

1. 为什么用 RRF 而不是加权分数融合？（分数尺度不可比；RRF 只看排名，稳健）
2. 为什么 BM25+向量要一起用？（语义兜底 vs 精确匹配互补；案例支持）
3. chunk_size / overlap 怎么选的？对指标影响？（可复现实验：改参数重跑评测）
4. 如何评估一个 RAG 系统？（检索指标+回答质量；本项目给出检索指标自动化方案）
5. Agent 相比「直接灌 prompt 问答」的优势？（工具编排、可扩展、可观测）
6. 如何防止幻觉？（检索引用+来源标注；知识库未命中时转人工）
7. 多轮对话怎么做的？（conversation_id + LangGraph checkpointer 记忆）
8. 如果文档是 PDF 扫描件怎么办？（OCR 扩展点——未来展望里可讲）

## 七、可以再讲的扩展点（供面试展示技术视野）

- 语义分块/标题加权、向量缓存、知识图谱、LLM-as-Judge 答案评测、多租户隔离、RAG 观测（token 成本/延迟）。

---

*关于真实性：以上量化数字以 `docs/eval_report.md` 实际输出为准，请勿手动改写夸大；面试官追问时能现场跑 `python scripts/eval_rag.py` 复现是最佳背书。*

---

## 八、可直接粘贴的简历段落

### 版本 A：详细版（推荐，投递算法/NLP/LLM 应用岗）

**企业知识库 AI 助手（Agentic RAG 智能问答系统）｜个人项目**
技术栈：FastAPI · LangGraph · LangChain · FAISS · rank-bm25 · RRF · BGE 系列模型 · SQLite · Docker · pytest

- 设计并实现基于 LangGraph ReAct 的企业知识库问答系统：Agent 自主编排「知识检索、数学计算、转人工」三类工具，支持多轮对话与 SSE 流式输出；
- 自研混合检索管线：FAISS 稠密向量 + BM25（jieba 中文分词）双路召回 → RRF 无参融合 → bge-reranker Cross-Encoder 精排（Top-20→Top-5），并以 LLM 查询改写（RAG-Fusion）提升口语化问题召回；
- 【量化】构建 24 题 × 9 篇企业语料检索评测集，对比 5 种检索策略（真实模型 API 运行、脚本可复现）：生产全流程 Hit@3 达 100%、MRR@5 达 97.9%、Top-1 达 95.8%，较纯向量检索分别提升 4.2 / 7.3 / 12.5 个百分点；
- 【工程化】FastAPI + Pydantic v2 + SQLite 实现文档与向量持久化，服务重启自动恢复索引；配置中心零硬编码密钥、fail-fast 校验；30 项单元测试全部离线通过；Docker 一键部署（健康检查 + 数据卷）。

### 版本 B：紧凑版（~100 字，投后端/通用研发岗）

基于 LangGraph Agent + 混合检索 RAG 的企业知识库问答系统：FAISS+BM25 双路召回、RRF 融合、bge-reranker 精排与查询改写，SQLite 持久化 + SSE 流式输出；自建 24 题评测集验证生产管线 Hit@3 100%、MRR@5 97.9%，较纯向量检索 MRR 提升 7.3 个百分点，指标一键可复现。

### 版本 C：英文版（如需英文简历）

**Enterprise Knowledge-Base AI Assistant (Agentic RAG) — Personal Project**
Tech stack: FastAPI · LangGraph · LangChain · FAISS · rank-bm25 · RRF · BGE models · SQLite · Docker · pytest

- Built an Agentic RAG Q&A system on LangGraph ReAct, where an agent autonomously orchestrates knowledge-base search / calculator / human-handover tools with multi-turn memory and SSE streaming.
- Designed a hybrid retrieval pipeline: FAISS dense vectors + BM25 (jieba tokenized) dual recall → RRF fusion → bge-reranker Cross-Encoder refinement (Top-20→Top-5), plus LLM query rewriting (RAG-Fusion) for paraphrased questions.
- [Quantified] Built a 24-question benchmark over 9 enterprise documents and compared 5 retrieval strategies with real model APIs (fully reproducible via script): the production pipeline reaches Hit@3 100%, MRR@5 97.9%, Top-1 95.8%, improving MRR@5 by +7.3pp over the dense-only baseline.
- [Engineering] FastAPI + Pydantic v2 + SQLite persistence (indices auto-restored on restart), centralized config with zero hardcoded secrets and fail-fast validation, 30 offline unit tests, Docker one-command deployment with healthcheck and volume.

### 使用提示

- 按岗位裁剪：算法岗保留「版本 A」，通用岗用「版本 B」；
- 数字与 `docs/eval_report.md`、`README.md` 完全一致，面试可现场 `python scripts/eval_rag.py` 复现。