# Bloomate

Bloomate 是一个以花园成长呈现个人目标与习惯的 AI 助手。你可以和园丁一起制定计划、记录行动，在遇到困难时预览调整建议，并决定是否采用。

当前为**本机开发版**：支持目标与习惯计划、今日行动、习惯打卡和补签、暂停恢复、计划调整建议以及阶段成长。账号、用户隔离、数据删除、正式上线和完整花园体验尚未完成；请仅在本机或受控环境使用，不要直接暴露到公网。

## 运行环境

- Python 3.12
- Node.js 22 和 npm
- 可选：兼容 OpenAI Chat Completions 接口的模型服务。本项目当前使用 DeepSeek Flash 验证。

## 本机启动

在仓库根目录，先安装后端依赖：

```bash
cd backend
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
```

编辑 `backend/.env`，启用真实模型时配置以下内容：

```dotenv
MODEL_ENABLED=1
MODEL_API_KEY=在这里填写自己的服务端密钥
MODEL_BASE_URL=https://api.deepseek.com
MODEL_NAME=deepseek-flash
MODEL_CALL_RESERVE_CENTS=10
```

请勿将真实密钥提交到 Git 或放到前端。此例的本地预算预留并不是服务商账单的硬限额；模型可用性、费用及数据处理规则以服务商为准。填写内容会发送到所配置的模型服务。

启动后端：

```bash
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

在另一个终端，从仓库根目录安装并启动前端：

```bash
cd frontend
npm ci
npm run dev -- --hostname 127.0.0.1 --port 3000
```

在同一台电脑打开 <http://127.0.0.1:3000>。如果后端端口不是 8000，启动前端时设置 `BLOOMATE_BACKEND_URL`，例如 `BLOOMATE_BACKEND_URL=http://127.0.0.1:18762 npm run dev`。

SQLite 数据默认保存在仓库根目录的 `data/`，该目录不会提交。测试生成计划时，可在后端启动命令前设置 `BLOOMATE_TEST_MODEL=1` 使用模拟模型；模拟结果不代表真实模型效果。

## 测试

```bash
cd backend
.venv/bin/python -m pytest -q
```

```bash
cd frontend
npm run lint
npm run typecheck
npm run build
```

仓库根目录运行 `scripts/restart-check.sh`，可使用隔离临时数据库和模拟模型检查服务重启后的数据恢复。真实模型的评测记录和规则见 [`docs/评测`](docs/评测/)。

## 当前边界

- 计划和模型建议需要用户确认；程序只校验明确的结构、数值和部分常见表述，不能保证识别所有自然语言错误。
- 目标步骤进度采用暂定均分规则，不代表现实目标已经完成。习惯开始日期、时区和单次完成标准当前不能修改。
- 当前使用本地默认用户，尚未实现多用户隔离、注册、游客数据过期清理或完整数据删除能力。
- 模型调用记录的本地预留金额与实际账单可能不同，不能替代服务商的额度控制。

产品需求和阶段设计位于 [`docs/`](docs/)，开发轮次见 [`tasks/plan.md`](tasks/plan.md)。

## 许可证

[MIT License](LICENSE)。保留原工具包的版权声明。
