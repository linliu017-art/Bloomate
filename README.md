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

> **开发中**：Bloomate 目前是本机开发版，适合开发和受控体验。邮箱登录、后端用户隔离及游客保存已实现；公开上线仍未完成，运营者公示和真实注销联调仍待验收，请勿直接暴露到公网。

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

- **分步制定目标与习惯**：先说想法，再确定安排，最后审阅并确认。桌面使用左侧步骤、右侧表单，沿用全站一致的居中留白；手机使用顶部步骤。
- **专注今日行动**：查看今天的目标步骤与习惯，完成后可以撤销。
- **调整节奏**：遇到困难时查看调整建议，也可以暂停、恢复或补记实际完成的行动。
- **积累你的花园**：植物卡片展示真实成长与下一步，进入详情管理行动；收藏按花与阶段成果展示，历史更正保留标记。
- **接收园丁关心**：在站内查看或忽略留言；园丁不会自动修改计划。
- **邮箱保存花园**：先游客体验，再用邮箱验证码登录并明确确认保存；游客数据保留 7 天，账号通过服务端会话隔离。
- **管理个人数据**：本人 JSON 导出、Bloomate 局部注销、服务条款与隐私页面已实现；公示与真实注销联调仍待验收。

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
   BLOOMATE_APP_MODE=local BLOOMATE_COOKIE_SECURE=0 .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 18762
   ```

4. 在另一个终端启动前端：

   ```bash
   cd frontend
   npm ci
   BLOOMATE_BACKEND_URL=http://127.0.0.1:18762 npm run dev -- --hostname 127.0.0.1 --port 3001
   ```

5. 在同一台电脑打开 <http://127.0.0.1:3001>。后端端口变化时同步设置 `BLOOMATE_BACKEND_URL`；前端来源变化时同步修改服务端 `BLOOMATE_ALLOWED_ORIGINS`。

邮箱认证默认关闭。使用自己的 CloudBase 环境配置邮箱认证与邮件代发后，在被忽略的 `backend/.env` 填写 `CLOUDBASE_ENV_ID`，并设置 `BLOOMATE_EMAIL_ENABLED=1`；缺配置时页面会明确提示，不使用固定演示验证码。请自行核实邮件服务的额度、费用与数据处理安排。

SQLite 数据默认保存在仓库根目录的 `data/`。后端启动时可设置 `BLOOMATE_TEST_MODEL=1` 使用模拟模型；模拟结果不能代表真实模型效果。

## 测试

```bash
cd backend
.venv/bin/python -m pytest -q
```

```bash
cd frontend
npm test -- --run
npm run lint
npm run typecheck
npm run build
```

仓库根目录运行 `bash scripts/restart-check.sh`，可使用隔离临时数据库及模拟邮件/模型检查重启后的业务与会话恢复。`scripts/restart_test_server.py` 仅供临时数据库验证，不能用于正式服务；模拟验证码不验证真实邮箱。

本次同步前的本机检查：后端 123/123、前端 10/10，类型、Lint、生产构建与重启恢复通过；真实模型通过正式界面生成、确认与刷新恢复。没有把模拟模型或模拟邮件当作真实服务验证。

## 项目现状

- 邮箱验证码登录、首次注册、可撤销会话、后端用户隔离、游客保存与过期清理已实现。
- 导出、Bloomate 局部注销与版本化条款已实现，运营者公示与一次性真实邮箱注销联调仍待验收。局部注销保留复用的 CloudBase 认证身份，不删除其他应用账号。
- 注销删除清单保存在业务 SQLite 旁的 `<数据库文件名>.erasures.jsonl`；恢复旧数据库时必须保留最新删除清单，避免已注销数据复活。
- 本机旧固定用户数据不会自动分配给新账号；部署、服务商安排与备份运维仍需独立核实。
- 模型建议和计划修改需要用户确认；程序只能校验结构、数值与部分常见表述，不能保证识别所有自然语言错误。
- 目标步骤进度采用暂定均分规则，不能单凭进度判断现实目标是否完成。
- 本地预算预留不等于服务商账单，也不能替代服务商的额度控制；模型费用与数据处理规则以服务商为准。

[`docs/`](docs/) 与 [`tasks/`](tasks/) 保留早期需求和开发记录；最新运行方式与实现状态以本页和源码为准。

## 许可证

[MIT License](LICENSE)。保留原工具包的版权声明。

<p align="center">🌱 每一步都值得被看见。</p>

