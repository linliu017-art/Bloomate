<p align="center">
  <img src="assets/bloomate-banner.svg" alt="Bloomate，让每一步慢慢开花" width="100%" />
</p>

<p align="center">
  <strong>把想做的事，变成可以开始的一小步。</strong><br />
  一位陪你制定计划、记录行动、见证成长的 AI 园丁。
</p>

<p align="center">
  <a href="#bloomate-是什么">项目介绍</a> ·
  <a href="#从一颗种子开始">使用流程</a> ·
  <a href="#本机启动">本机启动</a> ·
  <a href="#项目现状">项目现状</a>
</p>

> **开发中**：Bloomate 目前是本机开发版，适合开发和受控体验。账号、用户隔离和正式上线尚未完成，请勿直接暴露到公网。

## Bloomate 是什么

想开始一个目标或习惯时，最难的往往是第一步。Bloomate 帮你把想法整理成可调整的计划，把每天的行动记录下来，并用一株逐渐生长的植物陪伴这个过程。

园丁提供建议，计划由你确认；每次完成行动，你都能看见自己的进展。

## 从一颗种子开始

| 01 · 告诉园丁 | 02 · 开始行动 | 03 · 看见成长 |
| :--- | :--- | :--- |
| 写下目标或习惯，和园丁一起整理成计划。 | 在今日行动里完成、打卡，也可以撤销或补签。 | 每一步推动植物生长，阶段完成后开花并收藏。 |

## 一座属于你的花园

![草地上生长的雏菊、向日葵和郁金香](assets/garden-illustration-v2.svg)

为计划选择一株植物。随着行动积累，它会逐渐生长；阶段完成后，你可以收藏这朵花，或继续下一阶段。

## 现在可以做什么

- **制定目标与习惯**：让园丁根据你的想法和时间提出建议，确认后再开始。
- **专注今日行动**：查看今天的目标步骤与习惯，完成后可以撤销。
- **调整节奏**：遇到困难时查看调整建议，也可以暂停、恢复或补记实际完成的行动。
- **积累你的花园**：查看植物成长、阶段记录和收藏的花朵。
- **接收园丁关心**：在站内查看或忽略留言；园丁不会自动修改计划。

## 本机启动

**环境要求：** Python 3.12、Node.js 22 和 npm。真实模型需要自行配置兼容 OpenAI Chat Completions 接口的服务。

1. 在项目根目录安装后端依赖，并准备环境文件：

   ```bash
   cd backend
   python3.12 -m venv .venv
   .venv/bin/pip install -r requirements.txt
   cp .env.example .env
   ```

2. 如需使用真实模型，编辑 `backend/.env` 配置模型服务，例如：

   ```dotenv
   MODEL_ENABLED=1
   MODEL_API_KEY=在这里填写自己的服务端密钥
   MODEL_BASE_URL=https://api.deepseek.com
   MODEL_NAME=deepseek-flash
   MODEL_CALL_RESERVE_CENTS=10
   ```

   密钥仅保存在本地服务端，不要提交到仓库。填写的内容会发送给所配置的模型服务；本地预算预留不是服务商账单的硬上限。

3. 启动后端：

   ```bash
   .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
   ```

4. 在另一个终端启动前端：

   ```bash
   cd frontend
   npm ci
   npm run dev -- --hostname 127.0.0.1 --port 3000
   ```

5. 在同一台电脑打开 <http://127.0.0.1:3000>。若后端不是 8000 端口，启动前端时设置 `BLOOMATE_BACKEND_URL`。

SQLite 数据默认保存在仓库根目录的 `data/`。后端启动时可设置 `BLOOMATE_TEST_MODEL=1` 使用模拟模型；模拟结果不能代表真实模型效果。

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

仓库根目录运行 `scripts/restart-check.sh`，可使用隔离临时数据库和模拟模型检查重启后的数据恢复。真实模型评测资料见 [`docs/评测`](docs/评测/)。

## 项目现状

- 当前仅支持本地默认用户，尚未实现注册、多用户隔离、游客数据过期清理和完整数据删除。
- 模型建议和计划修改需要用户确认；程序只能校验结构、数值与部分常见表述，不能保证识别所有自然语言错误。
- 目标步骤进度采用暂定均分规则，不能单凭进度判断现实目标是否完成。
- 本地预算预留不等于服务商账单，也不能替代服务商的额度控制；模型费用与数据处理规则以服务商为准。

产品需求与阶段设计见 [`docs/`](docs/)，开发轮次见 [`tasks/plan.md`](tasks/plan.md)。

## 许可证

[MIT License](LICENSE)。保留原工具包的版权声明。

<p align="center">🌱 每一步都值得被看见。</p>
