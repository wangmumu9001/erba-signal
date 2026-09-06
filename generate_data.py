#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
二八择时网站 —— 数据生成脚本
拉取两指数日K线 → 计算比值/分位/状态/策略信号 → 输出 data.json
由 GitHub Actions 每天自动运行; 本地可手动运行预览
数据源: 东财优先, 腾讯备用 (双源容错)
"""
import json
import urllib.request
import urllib.parse
from datetime import datetime, timedelta, timezone

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
EM_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
TX_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"

CYB_SECID, CYB_TX = "0.399006", "sz399006"   # 创业板指
HDL_SECID, HDL_TX = "1.000922", "sh000922"   # 中证红利

THR_HI, THR_LO, FLOOR = 0.6, 0.4, 0.30       # 策略参数
BEIJING_TZ = timezone(timedelta(hours=8))
OUT_FILE = "data.json"


def fetch_em(secid):
    p = {"secid": secid, "fields1": "f1,f2,f3,f4,f5,f6",
         "fields2": "f51,f52,f53,f54,f55,f56,f57",
         "klt": "101", "fqt": "1", "beg": "20150101", "end": "20991231"}
    url = EM_URL + "?" + urllib.parse.urlencode(p)
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": "https://quote.eastmoney.com/"})
    with urllib.request.urlopen(req, timeout=20) as r:
        d = json.loads(r.read().decode("utf-8"))
    kls = (d.get("data") or {}).get("klines") or []
    if not kls:
        raise RuntimeError(f"东财 {secid} 空")
    return [k.split(",")[0] for k in kls], [float(k.split(",")[2]) for k in kls]


def fetch_tx(tx_code):
    rows = {}
    for s, e in [("2015-01-01", "2018-06-30"), ("2018-07-01", "2022-06-30"),
                 ("2022-07-01", "2099-12-31")]:
        param = f"{tx_code},day,{s},{e},1000,qfq"
        req = urllib.request.Request(TX_URL + "?" + urllib.parse.urlencode({"param": param}),
                                     headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=20) as r:
            d = json.loads(r.read().decode("utf-8"))
        data = (d.get("data") or {}).get(tx_code) or {}
        for ln in (data.get("qfqday") or data.get("day") or []):
            rows[ln[0]] = float(ln[2])
    if not rows:
        raise RuntimeError(f"腾讯 {tx_code} 空")
    dates = sorted(rows.keys())
    return dates, [rows[x] for x in dates]


def fetch_index():
    try:
        c = fetch_em(CYB_SECID)
        h = fetch_em(HDL_SECID)
        return c, h, "东财"
    except Exception as e:
        print(f"[WARN] 东财失败({e}), 切腾讯")
        c = fetch_tx(CYB_TX)
        h = fetch_tx(HDL_TX)
        return c, h, "腾讯"


def weights_history(ratios, defense_w):
    """带滞回的策略权重序列"""
    w = defense_w
    out = []
    for v in ratios:
        if v > THR_HI:
            w = defense_w
        elif v < THR_LO:
            t = defense_w + (THR_LO - v) / (THR_LO - FLOOR) * (0.80 - defense_w)
            t = min(max(t, defense_w), 0.80)
            if t > w:
                w = t
        out.append(round(w, 4))
    return out


def pct(value, series):
    return round(sum(1 for v in series if v < value) / len(series) * 100, 1)


def main():
    (cd, cc), (hd, hc), source = fetch_index()
    n = min(len(cd), len(hd))
    cd, cc, hd, hc = cd[-n:], cc[-n:], hd[-n:], hc[-n:]
    if cd[-1] != hd[-1]:
        print(f"[WARN] 两指数最新日期不一致: {cd[-1]} vs {hd[-1]}")

    ratios = [c / h for c, h in zip(cc, hc)]
    cyb, hdl, ratio = cc[-1], hc[-1], ratios[-1]
    chg = (ratio / ratios[-2] - 1) * 100 if len(ratios) > 1 else 0
    ma20 = sum(ratios[-20:]) / 20

    w_orig_hist = weights_history(ratios, 0.20)
    w_clear_hist = weights_history(ratios, 0.00)

    if ratio > THR_HI:
        zone, zone_cn = "defense", "防守区间"
    elif ratio < THR_LO:
        zone, zone_cn = "attack", "进攻区间"
    else:
        zone, zone_cn = "hold", "滞回区间"

    now_bj = datetime.now(BEIJING_TZ)
    week = "一二三四五六日"[now_bj.weekday()]

    # 近5年历史 (画图用)
    five_years_ago = now_bj.replace(year=now_bj.year - 5).strftime("%Y-%m-%d")
    hist5 = [
        {"date": d, "ratio": round(r, 4),
         "wo": w_orig_hist[i], "wc": w_clear_hist[i]}
        for i, (d, r) in enumerate(zip(cd, ratios)) if d >= five_years_ago
    ]

    data = {
        "generated": now_bj.strftime("%Y-%m-%d %H:%M"),
        "weekday": week,
        "source": source,
        "latest_date": cd[-1],
        "cyb": cyb, "hdl": hdl, "ratio": round(ratio, 4),
        "ratio_chg": round(chg, 2),
        "pct_1y": pct(ratio, ratios[-250:]),
        "pct_3y": pct(ratio, ratios[-750:]),
        "pct_all": pct(ratio, ratios),
        "ma20": round(ma20, 4),
        "zone": zone, "zone_cn": zone_cn,
        "w_original": w_orig_hist[-1], "w_clear": w_clear_hist[-1],
        "thr_hi": THR_HI, "thr_lo": THR_LO, "floor": FLOOR,
        "history": hist5,
    }

    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    print(f"✅ data.json 已生成 ({len(hist5)} 条历史) | 来源:{source} | 比值:{ratio:.4f} | 状态:{zone_cn}")


if __name__ == "__main__":
    main()
