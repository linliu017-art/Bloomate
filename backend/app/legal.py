"""Versioned, public legal documents tied to the product's actual behavior."""
import os
import re
from urllib.parse import urlsplit

from fastapi import Request
from pydantic import BaseModel, Field

from app import auth

VERSION = '2026-11-05-v1'
UPDATED = '2026-11-05'


def public_ready():
    name = os.environ.get('BLOOMATE_OPERATOR_NAME', '').strip()
    email = os.environ.get('BLOOMATE_CONTACT_EMAIL', '').strip()
    return bool(2 <= len(name) <= 120 and not any(x in name for x in ('待填写', '待补', '示例'))
                and re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', email) and len(email) <= 254)


def agreement_versions_valid(terms, privacy):
    if terms == privacy == VERSION:
        return True
    return os.environ.get('BLOOMATE_APP_MODE') == 'local' and terms == privacy == 'local-preview-v1'


def agreement_required(owner):
    if os.environ.get('BLOOMATE_APP_MODE') == 'local':
        return False
    with auth.connect() as db:
        return not bool(db.execute('''SELECT 1 FROM auth_policy_acceptances
            WHERE owner_id=? AND terms_version=? AND privacy_version=?''', (owner, VERSION, VERSION)).fetchone())


def document(heading, *paragraphs):
    return {'heading': heading, 'paragraphs': list(paragraphs)}


def documents():
    operator = os.environ.get('BLOOMATE_OPERATOR_NAME', '').strip() or None
    contact = os.environ.get('BLOOMATE_CONTACT_EMAIL', '').strip() or None
    contact_text = f'运营者：{operator}；联系邮箱：{contact}。' if public_ready() else '运营者名称和公开联系邮箱尚未补齐；当前仅用于本机受控验收，尚不作为公开服务的生效文本。'
    host = urlsplit(os.environ.get('MODEL_BASE_URL', '')).hostname
    model_text = ('当前计划生成请求直接发送至DeepSeek接口（api.deepseek.com）。'
                  if host == 'api.deepseek.com' else '当前模型服务配置尚需在公开上线前核实并公示；请勿输入真实私密内容。')
    return {'version': VERSION, 'updated_at': UPDATED, 'ready': public_ready(),
            'operator_name': operator, 'contact_email': contact,
            'terms': {'title': 'Bloomate 服务条款', 'sections': [
                document('一、服务与适用范围', contact_text, 'Bloomate为成年人提供目标与习惯规划、行动记录、花园成长和收藏功能。请在充分阅读本条款和隐私政策后决定是否使用；当前没有面向未成年人的服务。'),
                document('二、账号与使用方式', '可先以游客体验，也可用邮箱验证码登录；首次验证创建Bloomate账号。请保管邮箱和登录设备，不向他人泄露验证码。游客内容保留7天，到期后不能继续访问。注册和将体验内容保存到账户是两个步骤，只有确认保存成功的内容才进入账户。'),
                document('三、人工智能生成与用户确认', model_text, '生成内容可能不准确、不完整或不适合你的实际情况，不构成医疗、心理诊断、法律、金融或其他专业意见。请勿把本服务用于紧急求助或重要专业决策。请核对建议并自行决定是否采用；计划及调整在你确认后才生效，生成内容不代表你已完成行动。'),
                document('四、你的内容与使用规范', '你保留对合法提交内容享有的权利，并应有权向本服务提供这些内容。为完成你请求的功能，Bloomate会按隐私政策处理输入、生成结果和执行记录。请勿提交他人隐私、商业秘密、违法内容，或尝试越权访问其他用户数据、滥发验证码和破坏服务。'),
                document('五、数据控制与账号注销', '你可以在“账号与隐私”下载本账号的数据、管理园丁记忆与关心，并注销Bloomate账号。注销须新的邮箱验证码和明确确认；会删除本应用的账号、花园、历史记录、记忆及关联体验，并撤销本应用所有设备会话。注销不能撤销，你已自行下载的副本不受本应用控制。', 'CloudBase认证身份由现有云环境共用，注销Bloomate不会注销其他产品的账号或删除共享认证身份。需要处理共享认证记录时，请联系运营者单独核实；我们不会声称已经删除第三方或其他产品的记录。'),
                document('六、费用与服务可用性', '当前版本未设置面向用户的付费购买入口。未来如引入付费功能，会在购买前告知项目、价格和适用条件并由你另行确认，不因接受本条款自动订阅或扣费。服务可能因维护、供应商故障或网络中断暂不可用；我们会提供合理的错误提示和恢复途径。'),
                document('七、变更、终止与责任', '功能和规则的重要变更会通过页面提示或更新说明告知；涉及个人信息处理目的、方式或种类的重要变更，会依法重新征求同意。若终止服务，将说明数据导出和处理安排。本条款不排除或限制依法不得排除的用户权利或运营者责任。'),
                document('八、联系与争议处理', contact_text, '如有账号、内容或服务问题，可通过公示邮箱联系运营者。双方可先协商解决；协商不成，可依适用法律向有管辖权的人民法院寻求解决。本条款不影响你依法投诉、举报或行使其他权利。'),
            ]},
            'privacy': {'title': 'Bloomate 隐私政策', 'sections': [
                document('一、谁在处理信息', contact_text, '本政策说明Bloomate在你使用本应用时如何收集、使用、保存和删除信息。它不替代认证或模型服务商对其自身处理活动的说明。'),
                document('二、信息种类、用途与必要性', '登录和账号安全：邮箱交由CloudBase验证，Bloomate保存邮箱、内部账号映射和协议版本记录。验证码仅用于当次验证，不写入业务数据库或日志；会话凭证放入HttpOnly Cookie，数据库仅保存其摘要。', '业务功能：你填写的目标、习惯、时间约束、澄清回答，生成提案、确认计划、行动与打卡、成长和收藏记录，用于提供和恢复对应功能。你可以不填写可选字段。', '园丁记忆：长期偏好默认关闭，须你单独开启并确认记忆内容后才用于后续计划。你可查看、修改、删除记忆或关闭使用。园丁站内关心按你的设置和行动记录生成，不发送营销邮件。', '安全和成本：记录有限的请求来源摘要、限流时间、模型调用次数、耗时和用量，不把邮箱作为模型输入，不记录密码、验证码、密钥或完整用户文档。'),
                document('三、模型与认证服务', model_text, '生成计划时，会发送完成任务所需的输入、相关历史，以及你已授权并确认的偏好；邮箱、登录凭证和其他用户的数据不会由本应用附加给模型。生成服务商可能按其政策保留或处理请求，我们不承诺它们永不留存或永不用于训练。请勿提交敏感信息。', 'CloudBase处理邮箱认证、验证码发送及共享认证身份。本应用与其他产品共用云认证环境；Bloomate的业务数据库和权限判断独立执行，不能通过登录一个账号读取另一个账号的业务数据。实际公开上线前将核实服务器区域、服务商协议和数据处理安排，不作未经核实的跨境处理承诺。'),
                document('四、Cookie、本地缓存与第三方链接', '仅使用功能必需的会话Cookie以及本应用的界面缓存标记，不在localStorage保存认证令牌。退出、注销或切换主体时清理本应用私有缓存；不会清理其他网站的数据。当前未接入广告或第三方行为分析SDK。', '你自行安装的浏览器扩展和打开的第三方网站由其自身规则管理，可能读取或修改页面；本应用不控制这些扩展。'),
                document('五、保存期限与备份边界', '游客内容暂存7天，到期立即停止访问，运行中的任务处理结束后由清理流程删除。账号业务数据保留至你主动删除或注销；限流摘要保留24小时，过期的验证和会话记录按清理策略移除。', '注销会清除本应用在线库内的个人内容及识别关联；为避免删除重置总成本，保留不再关联账号的月份用量合计。为防止旧数据库恢复后账号复活，另外保留仅含随机内部编号摘要、操作号和时间的删除清单，并在恢复和启动时先执行清理。', '当前本机不自动创建包含注册账号的长期备份。管理员手工离线副本和你下载的导出文件不可能由网页即时远程擦除；有权访问副本的人须按删除清单和保留规则处理，不可未经清理重新投入服务。未来公开服务启用备份前，应明确公示保留期限并落实到运维流程。'),
                document('六、查阅、导出、更正与撤回', '登录后可在“账号与隐私”导出JSON，包含本人业务数据和协议同意记录，不含验证码、会话凭证、供应商令牌或其他人的数据。文件包含你的个人内容，请自行妥善保管。', '可在对应功能中更正计划、行动和记忆；关闭长期记忆后，后续生成不再附加已存偏好。你可以注销Bloomate账号撤回对本应用继续处理账号数据的同意。历史已发送给服务商的数据，需按该服务商的政策另行申请处理。', '如无法通过页面行使权利、需要处理共享认证记录，或希望解释某项处理，请通过公示邮箱联系运营者。我们会核实必要的身份信息、说明处理结果或拒绝理由；不会以无限扩大的身份材料要求阻碍你的权利。'),
                document('七、注销操作与安全保护', '注销前会展示删除范围，建议先导出；必须完成新的邮箱验证并输入确认文字。处理中有任务时会要求结束后重试。成功后撤销本应用的全部会话，同邮箱重新注册将获得新的空账号，不会恢复已删除内容。', '应用实施后端主体隔离、Origin与CSRF校验、验证码限流、凭证摘要和异常保护；正式部署须使用HTTPS。发生可能影响个人信息安全的事件时，会采取补救措施，并依法通知相关用户和主管部门。'),
                document('八、未成年人、政策更新与联系', '当前服务面向成年人。若发现误收集未成年人信息，请联系运营者核实处理。更新本政策时会提供新版本和日期；依法需要重新同意时，不会用旧版本记录代替新同意。', contact_text),
            ]},
            'sources': [{'label': 'CloudBase身份认证说明', 'url': 'https://docs.cloudbase.net/authentication-v2/method/email-login'},
                        {'label': 'DeepSeek隐私政策', 'url': 'https://cdn.deepseek.com/policies/zh-CN/deepseek-privacy-policy.html'}]}


class AgreementInput(BaseModel):
    terms_version: str = Field(max_length=40)
    privacy_version: str = Field(max_length=40)


def install(app):
    @app.get('/api/v1/legal')
    def legal_documents():
        return documents()

    @app.post('/api/v1/auth/account/agreements')
    def accept_agreements(data: AgreementInput, request: Request):
        owner = auth.require_account(request)
        if data.terms_version != VERSION or data.privacy_version != VERSION:
            raise auth.AuthError('AGREEMENT_CHANGED', '条款版本已更新，请重新阅读。', 409)
        with auth.connect() as db:
            db.execute('INSERT OR IGNORE INTO auth_policy_acceptances VALUES (?,?,?,?)',
                       (owner, VERSION, VERSION, auth.now()))
        return {'accepted': True, 'version': VERSION}
