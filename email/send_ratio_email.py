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
import smtplib
import urllib.request
import urllib.parse
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from email.header import Header
from email.utils import formataddr

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
        param = f"{tx_code},day,{start},{end},1000,qfq"
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
        tip = f"比值处于高位(>0.6), 按策略应清仓/减仓创业板(当前{1-w_clear:.0%}红利防守)。"
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


# ---------------- 邮件发送 ----------------
def send_mail(from_addr: str, auth_code: str, to_addrs: list[str],
              subject: str, body: str) -> None:
    msg = MIMEText(body, "plain", "utf-8")
    msg["From"] = formataddr((str(Header("二八择时日报", "utf-8")), from_addr))
    msg["To"] = ", ".join(to_addrs)
    msg["Subject"] = Header(subject, "utf-8")

    # QQ邮箱 SMTP SSL 465
    server = smtplib.SMTP_SSL("smtp.qq.com", 465, timeout=30)
    try:
        server.login(from_addr, auth_code)
        server.sendmail(from_addr, to_addrs, msg.as_string())
    finally:
        server.quit()


# ---------------- 主流程 ----------------
def main() -> int:
    # 1. 拉数据 (东财优先, 腾讯备用)
    cyb_dates, cyb_closes, hdl_dates, hdl_closes = fetch_index(
        CYB_SECID, "sz399006", HDL_SECID, "sh000922")
    if len(cyb_dates) != len(hdl_dates):
        print("[WARN] 两指数K线数量不一致, 取较小值对齐")
        n = min(len(cyb_dates), len(hdl_dates))
        cyb_dates, cyb_closes = cyb_dates[-n:], cyb_closes[-n:]
        hdl_dates, hdl_closes = hdl_dates[-n:], hdl_closes[-n:]

    # 2. 北京时间"今天"
    now_bj = datetime.now(BEIJING_TZ)
    today_str = now_bj.strftime("%Y-%m-%d")

    # 3. 判断是否交易日: 最新K线日期 == 今天
    #    设置环境变量 MAIL_FORCE_DATE=1 可跳过检测, 强制用最新交易日数据(用于测试预览)
    latest_date = cyb_dates[-1]
    force_date = os.environ.get("MAIL_FORCE_DATE", "").strip()
    if force_date:
        print(f"[FORCE] 跳过交易日检测, 用最新交易日 {latest_date} 的数据")
        today_str = latest_date  # 用最新交易日作为标题日期
        now_bj = datetime.strptime(latest_date, "%Y-%m-%d").replace(tzinfo=BEIJING_TZ)  # 星期也按该交易日
    elif latest_date != today_str:
        print(f"最新K线日期 {latest_date}, 今天 {today_str} → 非交易日(或数据未更新), 跳过发送")
        return 0

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
        print("提示: 配置 MAIL_FROM / MAIL_AUTH_CODE / MAIL_TO 环境变量后即真实发送")
        return 0

    to_addrs = [a.strip() for a in to_str.split(",") if a.strip()]
    try:
        send_mail(from_addr, auth_code, to_addrs, subject, body)
        print(f"✅ 邮件已发送 → {to_addrs}")
    except Exception as e:
        print(f"❌ 发送失败: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
