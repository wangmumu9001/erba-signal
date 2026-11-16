#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
二八择时日报 —— 每日邮件发送脚本
指标: 创业板指 / 中证红利

流程: 拉取两指数日K线 → 计算比值/分位/状态 → 生成邮件 → SMTP发送(QQ邮箱)
配置: 通过环境变量读取 (GitHub Actions 里配成 Secrets)
  MAIL_FROM       发件QQ邮箱地址, 如 xxx@qq.com
  MAIL_AUTH_CODE  QQ邮箱SMTP授权码 (非登录密码)
  MAIL_TO         收件人邮箱, 多个用逗号分隔
若 MAIL_FROM 未配置 → 进入 dry-run 模式, 只打印邮件内容不发信(便于调试)
"""
import os
import sys
import json
import tempfile
import traceback
import smtplib
import urllib.request
import urllib.parse
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.header import Header
from email.utils import formataddr
from html import escape as html_escape

# ---------------- 常量 ----------------
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"

CYB_SECID = "0.399006"   # 创业板指 (深市)
HDL_SECID = "1.000922"   # 中证红利 (沪市)

# 策略参数
THR_HI = 0.6   # 防守触发
THR_LO = 0.4   # 进攻触发
FLOOR = 0.30   # 渐进加仓满配点

BEIJING_TZ = timezone(timedelta(hours=8))  # 北京时间

# 幂等去重: 记录每个"已发送邮件"对应的数据日期, 保证同一数据日期的信一天至多一封
# (cron 迟到、手动补发等重复触发时, 命中记录即跳过, 不再轰炸收件人)
SENT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sent_dates.txt")


# ---------------- 数据获取 (东财优先, 腾讯备用) ----------------
def fetch_em(secid: str):
    """东财日K线 → (日期列表, 收盘价列表)"""
    params = {
        "secid": secid,
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57",
        "klt": "101", "fqt": "1",
        "beg": "20150101", "end": "20991231",
    }
    url = KLINE_URL + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Referer": "https://quote.eastmoney.com/"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    klines = (data.get("data") or {}).get("klines") or []
    if not klines:
        raise RuntimeError(f"东财 {secid} 返回空数据")
    dates = [ln.split(",")[0] for ln in klines]
    closes = [float(ln.split(",")[2]) for ln in klines]
    return dates, closes


def fetch_tx(tx_code: str):
    """腾讯备用源: 分页拼接全历史 → (日期列表, 收盘价列表)"""
    segments = [
        ("2015-01-01", "2018-06-30"),
        ("2018-07-01", "2022-06-30"),
        ("2022-07-01", "2099-12-31"),
    ]
    rows = {}
    base = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
    for start, end in segments:
        param = f"{tx_code},day,{start},{end},2000,qfq"
        req = urllib.request.Request(
            base + "?" + urllib.parse.urlencode({"param": param}),
            headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=20) as resp:
            d = json.loads(resp.read().decode("utf-8"))
        data = (d.get("data") or {}).get(tx_code) or {}
        klines = data.get("qfqday") or data.get("day") or []
        for ln in klines:
            rows[ln[0]] = float(ln[2])  # date -> close (腾讯索引2为收盘)
    if not rows:
        raise RuntimeError(f"腾讯 {tx_code} 返回空数据")
    dates = sorted(rows.keys())
    return dates, [rows[x] for x in dates]


def fetch_index(cyb_secid, cyb_tx, hdl_secid, hdl_tx):
    """双源获取两指数收盘序列"""
    try:
        c = fetch_em(cyb_secid)
        h = fetch_em(hdl_secid)
        print(f"[数据源] 东财: 创业板{len(c[0])}根 / 红利{len(h[0])}根")
        return c[0], c[1], h[0], h[1]
    except Exception as e:
        print(f"[WARN] 东财数据源失败({e}), 切换腾讯备用源")
        c = fetch_tx(cyb_tx)
        h = fetch_tx(hdl_tx)
        print(f"[数据源] 腾讯: 创业板{len(c[0])}根 / 红利{len(h[0])}根")
        return c[0], c[1], h[0], h[1]


# ---------------- 策略状态机 ----------------
def strategy_signal(x_series, defense_w: float):
    """
    带滞回的阈值策略, 返回 (最终权重, 状态, 说明)
    defense_w: 防守时创业板权重 (原版0.2 / 清仓版0.0)
    """
    state = "defense"
    w = defense_w
    for v in x_series:
        if v > THR_HI:
            state, w = "defense", defense_w
        elif v < THR_LO:
            state = "attack"
            t = defense_w + (THR_LO - v) / (THR_LO - FLOOR) * (0.80 - defense_w)
            t = min(max(t, defense_w), 0.80)
            if t > w:
                w = t
        # 0.4~0.6 之间: 维持前态(滞回)
    return w, state


def percentile(value: float, series: list[float]) -> float:
    return sum(1 for v in series if v < value) / len(series) * 100


# ---------------- 邮件内容 ----------------
def build_email_body(cyb: float, hdl: float, ratio: float, ratio_chg: float,
                     pct_1y: float, pct_3y: float, pct_all: float,
                     ma20: float, w_original: float, w_clear: float,
                     today: str, weekday_cn: str) -> str:
    """生成纯文本邮件正文"""

    # 状态判定(按当前比值区间 + 策略实际权重)
    if ratio > THR_HI:
        zone = "🔴 防守区间"
        tip = (f"比值处于高位(>0.6), 按策略应清仓/减仓创业板"
               f"(清仓版当前 {1-w_clear:.0%} 红利, 原版当前 {1-w_original:.0%} 红利)。")
    elif ratio < THR_LO:
        zone = "🟢 进攻区间"
        tip = f"比值处于低位(<0.4), 策略处于进攻状态, 已持有创业板{w_clear*100:.0f}%(随比值下降渐进加仓中)。"
    else:
        zone = "🟡 滞回区间"
        tip = f"比值处于0.4~0.6滞回带, 策略维持前一持仓不动作(当前创业板{w_clear*100:.0f}%)。"

    lines = [
        f"【二八择时日报】{today} {weekday_cn}",
        "",
        "──────── 核心数据 ────────",
        f"  创业板指: {cyb:,.2f}",
        f"  中证红利: {hdl:,.2f}",
        f"  比  值 : {ratio:.4f}   ({ratio_chg:+.2f}%)",
        "",
        "──────── 比值位置 ────────",
        f"  近1年分位: {pct_1y:.1f}%",
        f"  近3年分位: {pct_3y:.1f}%",
        f"  全历史分位: {pct_all:.1f}%",
        f"  比MA20  : {'上方' if ratio > ma20 else '下方'}",
        "",
        f"──────── 状态: {zone} ────────",
        f"  原版信号(20/80):  创业板 {w_original*100:.0f}% / 红利 {(1-w_original)*100:.0f}%",
        f"  清仓版信号(0/100): 创业板 {w_clear*100:.0f}% / 红利 {(1-w_clear)*100:.0f}%",
        "",
        "──────── 操作提示 ────────",
        f"  {tip}",
        f"  再次进攻条件: 比值跌破 {THR_LO} 后逐步加仓创业板至80%。",
        f"  防守确认条件: 比值升至 {THR_HI} 以上, 减仓至防御配置。",
        "",
        "（本邮件由 GitHub Actions 自动生成, 数据来源: 东方财富）",
    ]
    return "\n".join(lines)


# ---------------- 比值走势图 ----------------
def build_ratio_chart(dates: list[str], ratios: list[float], out_path: str) -> bool:
    """用每日比值序列生成全历史走势图 PNG, 成功返回 True (失败不阻断发信)"""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates
        from matplotlib import font_manager
    except ImportError as e:
        print(f"[WARN] 未安装 matplotlib({e}), 跳过走势图")
        return False

    # 中文字体: runner 未必有 CJK 字体, 找不到就用英文标签, 避免方块字
    installed = {f.name for f in font_manager.fontManager.ttflist}
    cjk = next((n for n in ("Noto Sans CJK SC", "Noto Sans SC", "Source Han Sans SC",
                            "Noto Sans CJK JP", "PingFang SC", "PingFang HK",
                            "Hiragino Sans GB", "Heiti SC", "STHeiti", "SimHei",
                            "Microsoft YaHei", "WenQuanYi Zen Hei",
                            "WenQuanYi Micro Hei", "Songti SC", "Arial Unicode MS")
                if n in installed), None)
    if cjk:
        plt.rcParams["font.sans-serif"] = [cjk, "DejaVu Sans"]
        plt.rcParams["axes.unicode_minus"] = False
        txt = {"title": "创业板指 / 中证红利 比值走势", "ratio": "比值",
               "ma": "MA20", "hi": "防守线 0.6", "lo": "进攻线 0.4", "cur": "当前"}
    else:
        print("[WARN] 未找到中文字体, 图表标签改用英文")
        txt = {"title": "ChiNext / CSI Dividend Ratio", "ratio": "Ratio",
               "ma": "MA20", "hi": "Defense 0.6", "lo": "Attack 0.4", "cur": "Now"}

    xs = [datetime.strptime(d, "%Y-%m-%d") for d in dates]
    ma20, acc = [], 0.0
    for i, v in enumerate(ratios):
        acc += v
        if i >= 20:
            acc -= ratios[i - 20]
        ma20.append(acc / min(i + 1, 20))

    fig, ax = plt.subplots(figsize=(9.6, 4.0), dpi=150)
    ax.axhspan(THR_LO, THR_HI, color="#dbe9ff", alpha=0.35, zorder=0)
    ax.plot(xs, ratios, color="#1f5fd0", lw=1.0, label=txt["ratio"], zorder=3)
    ax.plot(xs, ma20, color="#8a8a8a", lw=1.0, label=txt["ma"], zorder=2)
    ax.axhline(THR_HI, color="#d9534f", ls="--", lw=1.0, zorder=1)
    ax.axhline(THR_LO, color="#2e9e5b", ls="--", lw=1.0, zorder=1)
    # 阈值标签放在绘图区右侧外, 避免压住曲线
    ax.text(1.01, THR_HI, txt["hi"], color="#d9534f", fontsize=8,
            transform=ax.get_yaxis_transform(), ha="left", va="center", clip_on=False)
    ax.text(1.01, THR_LO, txt["lo"], color="#2e9e5b", fontsize=8,
            transform=ax.get_yaxis_transform(), ha="left", va="center", clip_on=False)
    ax.scatter([xs[-1]], [ratios[-1]], color="#1f5fd0", s=20, zorder=4)
    ax.annotate(f"{txt['cur']} {ratios[-1]:.3f}\n{dates[-1]}",
                xy=(xs[-1], ratios[-1]), xytext=(-90, 26), textcoords="offset points",
                fontsize=8, color="#1f5fd0",
                arrowprops=dict(arrowstyle="->", color="#1f5fd0", lw=0.8))

    ax.set_title(txt["title"], fontsize=12, pad=10)
    ax.set_ylabel(txt["ratio"], fontsize=9)
    ax.xaxis.set_major_locator(mdates.YearLocator())
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    ax.grid(axis="y", alpha=0.25)
    ax.set_xlim(xs[0], xs[-1])
    lo, hi = min(min(ratios), THR_LO), max(max(ratios), THR_HI)
    pad = (hi - lo) * 0.06
    ax.set_ylim(lo - pad, hi + pad)
    for sp in ("top", "right"):
        ax.spines[sp].set_visible(False)
    ax.legend(loc="upper left", fontsize=8, frameon=False)

    fig.tight_layout()
    fig.savefig(out_path, bbox_inches="tight")
    plt.close(fig)
    return True


def build_email_html(plain_body: str, has_chart: bool) -> str:
    """纯文本正文 → HTML 版正文 (图表以 cid:ratio_chart 内嵌)"""
    first, _, rest = plain_body.partition("\n")
    chart = ('<div style="margin:14px 0">'
             '<img src="cid:ratio_chart" alt="创业板/中证红利比值走势" '
             'style="width:100%;max-width:820px;height:auto;border:0"></div>'
             ) if has_chart else ""
    return (
        '<!DOCTYPE html><html><body style="margin:0;padding:18px;background:#f6f7f9">'
        '<div style="max-width:860px;margin:0 auto;background:#fff;border:1px solid #e5e7eb;'
        'border-radius:10px;padding:20px 24px">'
        f'<h2 style="margin:0 0 6px;font-size:18px;color:#1f2937">{html_escape(first)}</h2>'
        f'{chart}'
        '<div style="font-family:Menlo,Consolas,\'Courier New\',monospace;font-size:13px;'
        f'line-height:1.65;color:#1f2937;white-space:pre-wrap">{html_escape(rest)}</div>'
        '</div></body></html>'
    )


# ---------------- 邮件发送 ----------------
def send_mail(from_addr: str, auth_code: str, to_addrs: list[str],
              subject: str, body: str, html_body: str | None = None,
              chart_path: str | None = None) -> None:
    if html_body:
        msg = MIMEMultipart("related")
        alt = MIMEMultipart("alternative")
        alt.attach(MIMEText(body, "plain", "utf-8"))
        alt.attach(MIMEText(html_body, "html", "utf-8"))
        msg.attach(alt)
        if chart_path and os.path.exists(chart_path):
            with open(chart_path, "rb") as f:
                img = MIMEImage(f.read(), _subtype="png")
            img.add_header("Content-ID", "<ratio_chart>")
            img.add_header("Content-Disposition", "inline", filename="ratio_chart.png")
            msg.attach(img)
    else:
        msg = MIMEText(body, "plain", "utf-8")
    msg["From"] = formataddr((str(Header("二八择时日报", "utf-8")), from_addr))
    msg["To"] = ", ".join(to_addrs)
    msg["Subject"] = Header(subject, "utf-8")

    # QQ邮箱 SMTP SSL 465
    print("[SMTP] 连接 smtp.qq.com:465 ...", flush=True)
    server = smtplib.SMTP_SSL("smtp.qq.com", 465, timeout=30)
    try:
        print("[SMTP] 已连接, 登录中 ...", flush=True)
        server.login(from_addr, auth_code)
        print("[SMTP] 登录成功, 投递中 ...", flush=True)
        server.sendmail(from_addr, to_addrs, msg.as_string())
        print("[SMTP] 投递成功", flush=True)
    finally:
        try:
            server.quit()
        except Exception as e:  # 别让 quit 的异常掩盖真实错误
            print(f"[SMTP] quit 异常: {type(e).__name__}: {e}", flush=True)


# ---------------- 幂等去重 ----------------
def already_sent(sent_file: str, data_date: str) -> bool:
    """该数据日期的信是否已发送过(sent_dates.txt 每行一个日期)"""
    try:
        with open(sent_file, encoding="utf-8") as f:
            return data_date in {ln.strip() for ln in f if ln.strip()}
    except FileNotFoundError:
        return False


def mark_sent(sent_file: str, data_date: str) -> None:
    """发送成功后记录该数据日期"""
    with open(sent_file, "a", encoding="utf-8") as f:
        f.write(data_date + "\n")


# ---------------- 主流程 ----------------
def main() -> int:
    # 1. 拉数据 (东财优先, 腾讯备用)
    cyb_dates, cyb_closes, hdl_dates, hdl_closes = fetch_index(
        CYB_SECID, "sz399006", HDL_SECID, "sh000922")
    # 按日期取交集对齐 (不能按长度截断: 任一指数缺一天都会让后续日期整体错位)
    cyb_map = dict(zip(cyb_dates, cyb_closes))
    hdl_map = dict(zip(hdl_dates, hdl_closes))
    common = sorted(set(cyb_map) & set(hdl_map))
    if not common:
        print("[ERROR] 两指数无共同交易日, 无法计算比值")
        return 1
    if len(common) != len(cyb_dates) or len(common) != len(hdl_dates):
        print(f"[WARN] 两指数日期不一致: 创业板{len(cyb_dates)}根 / 红利{len(hdl_dates)}根, "
              f"按共同交易日 {len(common)} 根对齐 "
              f"(创业板缺 {len(cyb_dates)-len(common)} 天, 红利缺 {len(hdl_dates)-len(common)} 天)")
    cyb_dates, cyb_closes = common, [cyb_map[d] for d in common]
    hdl_dates, hdl_closes = common, [hdl_map[d] for d in common]

    # 2. 北京时间"今天"
    now_bj = datetime.now(BEIJING_TZ)
    today_str = now_bj.strftime("%Y-%m-%d")

    # 3. 只在最新K线已收盘后发送
    #    - 数据日期晚于今天 → 数据异常, 不发
    #    - 数据日期就是今天 → 必须已过 15:10
    #    - 数据日期早于今天 → 最近一个历史交易日, 视为已收盘
    #      (GitHub cron 延迟跨日时仍可补发该交易日日报, 重复由 sent_dates.txt 兜底)
    #    设置 MAIL_FORCE_DATE=1 可跳过检测, 强制用最新交易日数据(用于测试预览)
    latest_date = cyb_dates[-1]
    force_date = os.environ.get("MAIL_FORCE_DATE", "").strip().lower() in ("1", "true", "yes")
    if force_date:
        print(f"[FORCE] 跳过收盘检测, 用最新交易日 {latest_date} 的数据")
    elif latest_date > today_str:
        print(f"[SKIP] 最新K线 {latest_date} 晚于今天({today_str}), 数据异常 → 不发")
        return 0
    elif latest_date == today_str and (now_bj.hour, now_bj.minute) < (15, 10):
        print(f"[SKIP] 北京时间 {now_bj:%H:%M} 未到 15:10, 当天尚未收盘 → 不发")
        return 0
    # 标题/正文/去重统一用数据日期, 补发时不会写错日期
    today_str = latest_date
    now_bj = datetime.strptime(today_str, "%Y-%m-%d").replace(tzinfo=BEIJING_TZ)

    # 4. 计算指标
    closes_zip = list(zip(cyb_closes, hdl_closes))
    ratios = [c / h for c, h in closes_zip]
    cyb, hdl = cyb_closes[-1], hdl_closes[-1]
    ratio = ratios[-1]
    ratio_prev = ratios[-2]
    ratio_chg = (ratio / ratio_prev - 1) * 100

    # 分位 (近1年250日, 近3年750日, 全历史)
    pct_1y = percentile(ratio, ratios[-250:])
    pct_3y = percentile(ratio, ratios[-750:])
    pct_all = percentile(ratio, ratios)

    # MA20
    ma20 = sum(ratios[-20:]) / 20

    # 策略信号 (用完整历史比值序列跑状态机, 含滞回记忆)
    w_original, _ = strategy_signal(ratios, 0.20)
    w_clear, _ = strategy_signal(ratios, 0.00)

    # 5. 生成邮件
    weekday_cn = "一二三四五六日"[now_bj.weekday()]
    body = build_email_body(cyb, hdl, ratio, ratio_chg, pct_1y, pct_3y, pct_all,
                            ma20, w_original, w_clear, today_str, weekday_cn)
    subject = f"【二八择时日报】{today_str} · 比值 {ratio:.3f}"

    # 走势图: 用同一份每日比值序列现场生成, 失败不影响文字邮件
    chart_path = None
    try:
        chart_dir = tempfile.mkdtemp(prefix="erba_chart_")
        p = os.path.join(chart_dir, "ratio_chart.png")
        if build_ratio_chart(cyb_dates, ratios, p):
            chart_path = p
            print(f"[图表] 已生成 {p} ({os.path.getsize(p) // 1024} KB)")
    except Exception as e:
        print(f"[WARN] 走势图生成失败: {type(e).__name__}: {e} (邮件将不带图)")
    body_html = build_email_html(body, has_chart=bool(chart_path))

    # 6. 发送 或 dry-run
    from_addr = os.environ.get("MAIL_FROM", "").strip()
    auth_code = os.environ.get("MAIL_AUTH_CODE", "").strip()
    to_str = os.environ.get("MAIL_TO", "").strip()

    if not from_addr or not auth_code or not to_str:
        print("=" * 60)
        print("[DRY-RUN] 未配置邮件参数, 以下为邮件内容:")
        print("=" * 60)
        print(f"主题: {subject}")
        print("-" * 60)
        print(body)
        print("=" * 60)
        if chart_path:
            preview = os.path.join(os.path.dirname(chart_path), "email_preview.html")
            with open(preview, "w", encoding="utf-8") as f:
                f.write(body_html.replace("cid:ratio_chart", "ratio_chart.png"))
            print(f"[DRY-RUN] 走势图: {chart_path}")
            print(f"[DRY-RUN] HTML 预览: {preview}")
        print("提示: 配置 MAIL_FROM / MAIL_AUTH_CODE / MAIL_TO 环境变量后即真实发送")
        return 0

    to_addrs = [a.strip() for a in to_str.split(",") if a.strip()]

    # 幂等去重: 该数据日期的信已发过 → 跳过 (保证重复触发也不重复发信)
    if already_sent(SENT_FILE, today_str):
        print(f"[DEDUP] {today_str} 的日报已发送过, 跳过本次(如需强制重发, 删 sent_dates.txt 中对应行)")
        return 0

    try:
        send_mail(from_addr, auth_code, to_addrs, subject, body,
                  html_body=body_html, chart_path=chart_path)
        mark_sent(SENT_FILE, today_str)
        print(f"✅ 邮件已发送 → {to_addrs} (已记录 {today_str}, 防重复)")
    except Exception as e:
        traceback.print_exc()
        print(f"❌ 发送失败: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
