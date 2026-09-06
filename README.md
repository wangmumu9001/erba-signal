# 二八择时信号 · 网页 + 邮件

创业板指 ÷ 中证红利 的择时信号, 每天收盘后自动更新网页并在下午发邮件。

## 两个定时任务 (GitHub Actions)

| 时间 (北京) | 内容 | 脚本 |
|---|---|---|
| 16:00 周一~五 | 邮件 → QQ邮箱 | `email/send_ratio_email.py` |
| 16:05 周一~五 | 更新数据并部署 GitHub Pages | `generate_data.py` + `update_page.yml` |

- 网页地址: `https://<owner>.github.io/<repo>/` (本仓库根目录 `index.html` + `data.json`)
- 双数据源容错: 东财优先, 腾讯备用; A股休市由脚本自动跳过(邮件)/数据不变(网页)

## 配置 Secrets (邮件用, 网页不需要)

| Secret | 含义 |
|---|---|
| `MAIL_FROM` | 发件 QQ 邮箱 |
| `MAIL_AUTH_CODE` | QQ 邮箱 SMTP 授权码(16位, mail.qq.com 生成) |
| `MAIL_TO` | 收件邮箱, 多个用逗号分隔 |

## 手动测试

- 网页: Actions → **更新网页数据并部署** → Run workflow
- 邮件: Actions → **二八择时日报** → Run workflow (配好 Secrets 后)
- 本地 dry-run 预览邮件内容: `MAIL_FORCE_DATE=1 python3 email/send_ratio_email.py`
