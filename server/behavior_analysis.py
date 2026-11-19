"""WorkPulse — Behavior: análise L2 (detectores) + pacote pronto para o painel.

Fonte única da análise: usada pelo servidor (job que gera o pacote) e pelo behavior-lab.
Entrada: payloads de /v1/behavior/day (um por dia) + lista de pessoas.
Saída: build_pack() -> {"meta", "pessoas", "sinais", "dias", "seguranca"} (mesmo formato do painel).
Funções puras: nada aqui toca banco, rede ou disco.
"""
import re, statistics as st
from collections import defaultdict, Counter
from datetime import datetime, timedelta, date, timezone

# ----------------------------------------------------------------- parametros
CFG = {
    "tz_offset_h": -3,
    "biz_start": 8, "biz_end": 18,         # horario comercial (h local)
    "night_start": 0, "night_end": 5,      # madrugada
    "min_day_active_min": 30,              # dia com menos que isso = "nao trabalhou"
    "baseline_min_days": 5,                # dias uteis p/ ter baseline pessoal
    "dev_min_minutes": 90,                 # desvio minimo de horario p/ sinal
    "dev_mad_k": 3.0,                      # ... e > k * MAD
    "warmup_lag_min": 60,                  # "loga cedo, comeca depois"
    "effective_window_min": 15, "effective_active_min": 10, "effective_work_ratio": 0.7,
    "nonwork_share": 0.20, "nonwork_min_day": 60,
    "long_run_min": 60,                    # mesma janela/titulo sem troca
    "night_active_min": 30,
    "offhours_active_min": 120, "offday_active_min": 60,
    "overload_day_h": 10.0, "overload_days": 3,
    "no_break_min_jornada_h": 7.0, "no_break_max_pause_min": 15,
    "trend_drop": 0.25,
}

NONWORK_RULES = [   # (classe, regex em app+titulo)  -- simula a classificacao por IA
    ("video",    r"youtube|netflix|twitch|prime video|primevideo|disney\+|globoplay|max\.com|hbo ?max|vimeo|kick\.com|crunchyroll|star\+"),
    ("noticia",  r"\bg1\b|globo\.com|uol|folha|estad[aã]o|terra\.com|r7|cnn|metr[oó]poles|poder360|infomoney|valor econ|bbc|ge\.globo|lance!|espn"),
    ("social",   r"instagram|facebook|tiktok|twitter|x\.com|\breddit|pinterest|threads|kwai"),
    ("compras",  r"mercado ?livre|shopee|amazon\.com|amazon\.com\.br|aliexpress|magazine ?luiza|magalu|americanas|shein|temu|olx"),
    ("jogos",    r"steam|epic games|roblox|league of legends|valorant|minecraft|chess\.com|poki"),
    ("musica",   r"spotify|deezer|soundcloud"),
]
WORK_HINT = [
    ("reuniao",  r"teams|zoom|google meet|meet\.google|\bmeet:|webex|whereby|gotomeeting|bluejeans|reuni[aã]o"),
    ("ia",       r"claude|chatgpt|copilot|gemini|cursor"),
    ("comunic",  r"slack|outlook|gmail|telegram|e-?mail"),
]
NONWORK_RE = [(c, re.compile(p, re.I)) for c, p in NONWORK_RULES]
WORK_RE = [(c, re.compile(p, re.I)) for c, p in WORK_HINT]
NONWORK_CLASSES = {c for c, _ in NONWORK_RULES} - {"musica"}

SIG_INFO = {
    "schedule_deviation": ("Horario fora do padrao", "Inicio ou fim do dia muito diferente do padrao da propria pessoa"),
    "warmup_lag":         ("Comeca tarde apos ligar", "Maquina ativa cedo, mas o trabalho efetivo so comeca bem depois"),
    "night_work":         ("Atividade de madrugada", "Atividade relevante entre 00h e 05h"),
    "long_static":        ("Muito tempo parado numa janela", "Mesmo app/titulo por muito tempo, sem trocar e sem AFK"),
    "nonwork_share":      ("Video, noticias, compras ou jogos", "Parcela alta do tempo em video, noticias, compras ou jogos"),
    "social_media":       ("Uso de redes sociais", "Tempo alto em redes sociais (WhatsApp nao entra)"),
    "low_activity":       ("Baixa atividade", "Tempo ativo bem abaixo do normal do departamento"),
    "hollow_day":         ("Presente mas pouco ativo", "Expediente longo com pouco tempo realmente ativo"),
    "absent_days":        ("Dias uteis sem atividade", "Dias uteis sem nenhuma atividade no meio do periodo, sem ferias marcadas"),
    "hybrid_gap":         ("Ritmo diferente casa x escritorio", "Tempo ativo medio bem diferente entre dias presenciais e dias em casa (pode ter motivo legitimo)"),
    "whatsapp_deviation": ("WhatsApp fora do padrao", "Dia com WhatsApp bem acima do normal da propria pessoa (WhatsApp e produtivo; isto so aponta desvio)"),
    "security_risk":      ("Risco de seguranca", "Acesso a sites e ferramentas que representam risco (compartilhamento, acesso remoto, VPN/Tor, pirataria, apostas...)"),
    "offhours":           ("Trabalho fora do horario", "Atividade relevante fora do horario comercial, fim de semana ou feriado"),
    "overload":           ("Sobrecarga sustentada", "Varios dias com jornada muito longa (risco de esgotamento)"),
    "no_break":           ("Sem pausa", "Jornadas longas sem nenhuma pausa significativa"),
    "fragmentation":      ("Muita troca de contexto", "Trocas de aplicativo por hora muito acima do padrao do time"),
    "engagement_drop":    ("Queda de engajamento", "Tempo ativo caindo de forma consistente no periodo"),
    "vacation_activity":  ("Ativo durante ferias", "Marcado como de ferias, mas com atividade"),
}
SEV_ORDER = {"alta": 0, "media": 1, "baixa": 2}

# ----------------------------------------------------------------- utilidades
_frac = re.compile(r"(\.\d{6})\d+")
def parse_ts(s):
    s = s.replace("Z", "+00:00").replace(" ", "T")
    s = _frac.sub(r"\1", s)
    d = datetime.fromisoformat(s)
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc).replace(tzinfo=None) + timedelta(hours=CFG["tz_offset_h"])

def easter(y):
    a=y%19;b=y//100;c=y%100;d=b//4;e=b%4;f=(b+8)//25;g=(b-f+1)//3;h=(19*a+b-d-g+15)%30
    i=c//4;k=c%4;l=(32+2*e+2*i-h-k)%7;m=(a+11*h+22*l)//451;mo=(h+l-7*m+114)//31;da=((h+l-7*m+114)%31)+1
    return date(y,mo,da)
def holidays(y):
    e = easter(y)
    fx = [(1,1),(4,21),(5,1),(9,7),(10,12),(11,2),(11,15),(11,20),(12,25)]
    hs = {date(y,m,d) for m,d in fx}
    hs |= {e-timedelta(days=2), e-timedelta(days=48), e-timedelta(days=47), e+timedelta(days=60)}
    return hs
_HOL = {}
def is_offday(d):
    if d.year not in _HOL: _HOL[d.year] = holidays(d.year)
    return d.weekday() >= 5 or d in _HOL[d.year]

def classify(app, title):
    txt = f"{app} {title}"
    for c, rx in NONWORK_RE:
        if rx.search(txt): return c
    for c, rx in WORK_RE:
        if rx.search(txt): return c
    return ""

def hm(minutes):
    if minutes is None: return "-"
    minutes = int(round(minutes)); s = "-" if minutes < 0 else ""; minutes = abs(minutes)
    return f"{s}{minutes//60}h{minutes%60:02d}"
def clock(m):
    if m is None: return "-"
    m = int(round(m)); return f"{(m//60)%24:02d}:{m%60:02d}"
def med(xs): return st.median(xs) if xs else None
def mad(xs):
    if len(xs) < 2: return 0.0
    m = st.median(xs); return st.median([abs(x-m) for x in xs])

# ----------------------------------------------------------------- L1 features
def day_features(day, items):
    day0 = datetime(day.year, day.month, day.day)
    act = [0.0]*1440; afkm = [0.0]*1440; workm = [0.0]*1440; nwm = [0.0]*1440
    cats = Counter(); classes = Counter(); apps = Counter()
    runs = []
    for it in items:
        try: t0 = parse_ts(it["start_ts"])
        except Exception: continue
        dur = float(it.get("total_sec") or 0)
        if dur <= 0: continue
        is_afk = (it.get("type") == "AFK" or it.get("category") == "afk")
        app = it.get("app") or ""; title = it.get("title") or ""
        cls = "" if is_afk else classify(app, title)
        nonwork = (not is_afk) and (cls in NONWORK_CLASSES or it.get("category") == "unproductive")
        runs.append({"t0": t0, "dur": dur, "afk": is_afk, "app": app, "title": title,
                     "cat": it.get("category"), "cls": cls, "nonwork": nonwork})
        # distribui por minuto
        s = (t0 - day0).total_seconds(); e = s + dur
        s = max(0.0, s); e = min(86400.0, e)
        m = int(s // 60)
        while s < e and m < 1440:
            seg = min(e, (m+1)*60) - s
            if is_afk: afkm[m] += seg
            else:
                act[m] += seg
                if nonwork: nwm[m] += seg
                else: workm[m] += seg
            s += seg; m += 1
        if not is_afk:
            cats[it.get("category") or "uncategorized"] += dur
            if cls: classes[cls] += dur
            apps[app.lower()] += dur
    active = sum(act)
    f = {"day": day, "active_sec": active, "offday": is_offday(day)}
    if active < CFG["min_day_active_min"]*60:
        f["worked"] = False; return f, runs
    f["worked"] = True
    mins = [i for i in range(1440) if act[i] > 0]
    first, last = mins[0], mins[-1] + 1
    f["first_min"], f["last_min"] = first, last
    f["jornada_min"] = last - first
    f["afk_in_sec"] = sum(afkm[first:last])
    # inicio efetivo: 1a janela de 15 min com >=10 min ativos e >=70% "trabalho"
    W = CFG["effective_window_min"]; eff = None
    for s in range(first, max(first+1, last-W+1)):
        a = sum(act[s:s+W]); w = sum(workm[s:s+W])
        if a >= CFG["effective_active_min"]*60 and w >= CFG["effective_work_ratio"]*a:
            eff = s; break
    f["effective_min"] = eff
    f["by_hour"] = [sum(act[h*60:(h+1)*60]) for h in range(24)]
    f["nw_by_hour"] = [sum(nwm[h*60:(h+1)*60]) for h in range(24)]
    f["night_sec"] = sum(act[CFG["night_start"]*60:CFG["night_end"]*60])
    bs, be = CFG["biz_start"]*60, CFG["biz_end"]*60
    f["offhours_sec"] = active if f["offday"] else (sum(act[:bs]) + sum(act[be:]))
    f["nonwork_sec"] = sum(nwm)
    f["classes"] = dict(classes); f["cats"] = dict(cats)
    f["top_apps"] = apps.most_common(5)
    # pausas, trocas, maior bloco estatico
    ar = sorted([r for r in runs if not r["afk"]], key=lambda r: r["t0"])
    switches = sum(1 for a, b in zip(ar, ar[1:]) if a["app"].lower() != b["app"].lower())
    f["switch_per_h"] = switches / max(active/3600, 0.5)
    pauses = [r["dur"] for r in runs if r["afk"]
              and first <= (r["t0"]-day0).total_seconds()/60 <= last]
    f["longest_break_min"] = (max(pauses)/60) if pauses else 0.0
    lr = max(ar, key=lambda r: r["dur"]) if ar else None
    f["longest_run"] = ({"min": lr["dur"]/60, "app": lr["app"], "title": lr["title"][:90],
                         "at": (lr["t0"]-day0).total_seconds()/60, "cls": lr["cls"]} if lr else None)
    f["meeting_sec"] = classes.get("reuniao", 0)
    return f, runs

# ----------------------------------------------------------------- L2 detectores
def sig(user, day, typ, sev, msg, evid=None, score=0.0):
    return {"user_id": user, "day": day.isoformat() if day else "", "type": typ, "sev": sev,
            "msg": msg, "evid": evid or "", "score": round(score, 2)}

def detect_user(uid, feats, ucfg):
    S = []
    wd = [f for f in feats if f["worked"] and not f["offday"]]
    by_day = {f["day"]: f for f in feats}
    for f in feats:
        if not f["worked"]: continue
        d = f["day"]
        # baseline pessoal (exclui o proprio dia); prefere mesmo dia da semana
        others = [g for g in wd if g["day"] != d]
        same = [g for g in others if g["day"].weekday() == d.weekday()]
        base = same if len(same) >= 3 else others
        if not f["offday"] and len(others) >= CFG["baseline_min_days"]:
            for key, label in (("first_min", "inicio"), ("last_min", "fim")):
                xs = [g[key] for g in base]; m = med(xs); md = mad(xs)
                dev = f[key] - m
                thr = max(CFG["dev_min_minutes"], CFG["dev_mad_k"] * md * 1.4826)
                if abs(dev) >= thr:
                    sev = "alta" if abs(dev) >= 2*thr and abs(dev) >= 150 else "media"
                    quando = "mais tarde" if dev > 0 else "mais cedo"
                    ref = f"das {['segundas','tercas','quartas','quintas','sextas','sabados','domingos'][d.weekday()]}" if base is same else "dos dias uteis"
                    S.append(sig(uid, d, "schedule_deviation", sev,
                        f"{label.capitalize()} as {clock(f[key])}, {hm(abs(dev))} {quando} que o padrao {ref} ({clock(m)})",
                        f"baseline={clock(m)} desvio={hm(dev)} limiar={hm(thr)} n={len(base)}", abs(dev)/thr))
        # comeca tarde
        if f.get("effective_min") is not None:
            lag = f["effective_min"] - f["first_min"]
            lags = [g["effective_min"] - g["first_min"] for g in wd if g["day"] != d and g.get("effective_min") is not None]
            ml = med(lags)
            if lag >= CFG["warmup_lag_min"] and (ml is None or lag >= ml + 45):
                S.append(sig(uid, d, "warmup_lag", "media" if lag < 120 else "alta",
                    f"Maquina ativa as {clock(f['first_min'])}, trabalho efetivo so as {clock(f['effective_min'])} ({hm(lag)} depois)",
                    f"lag mediano da pessoa={hm(ml)}", lag/CFG["warmup_lag_min"]))
        # madrugada
        if f["night_sec"] >= CFG["night_active_min"]*60:
            lr = f["longest_run"]; extra = ""
            if lr and lr["at"] < CFG["night_end"]*60 + 60 and lr["min"] >= CFG["long_run_min"]:
                extra = f"; maior bloco: {hm(lr['min'])} em {lr['app']} sem trocar"
            S.append(sig(uid, d, "night_work", "alta" if f["night_sec"] >= 7200 else "media",
                f"{hm(f['night_sec']/60)} ativo entre 00h e 05h{extra}", "", f["night_sec"]/1800))
        # bloco estatico longo
        lr = f["longest_run"]
        if lr and lr["min"] >= CFG["long_run_min"] and lr["cls"] != "reuniao":
            if lr["cls"] == "ia":
                sev = "baixa"; extra = " (sessao longa em ferramenta de IA)"
            else:
                sev = "media" if (lr["cls"] in NONWORK_CLASSES or lr["min"] >= 120) else "baixa"; extra = ""
            S.append(sig(uid, d, "long_static", sev,
                f"{hm(lr['min'])} seguidos em {lr['app']} as {clock(lr['at'])} sem trocar de janela nem ficar AFK{extra}",
                f"titulo: {lr['title']}", lr["min"]/CFG["long_run_min"]))
        # nao-trabalho
        soc = f["classes"].get("social", 0)
        if not f["offday"] and (soc >= 45*60 or (soc >= 20*60 and soc / f["active_sec"] >= 0.15)):
            S.append(sig(uid, d, "social_media", "alta" if soc >= 90*60 else "media",
                f"{hm(soc/60)} em redes sociais ({soc / f['active_sec']:.0%} do tempo ativo)", "", soc/2700))
        nwx = max(0, f["nonwork_sec"] - soc)
        share = nwx / f["active_sec"]
        if not f["offday"] and (share >= CFG["nonwork_share"] or nwx >= CFG["nonwork_min_day"]*60):
            top = sorted(((c, s) for c, s in f["classes"].items() if c in NONWORK_CLASSES and c != "social"), key=lambda x: -x[1])[:3]
            det = ", ".join(f"{c} {hm(s/60)}" for c, s in top)
            S.append(sig(uid, d, "nonwork_share", "alta" if share >= 0.35 else "media",
                f"{share:.0%} do tempo ativo ({hm(nwx/60)}) fora do trabalho" + (f": {det}" if det else ""),
                "", share/CFG["nonwork_share"]))
        # fora do horario
        if f["offhours_sec"] >= (CFG["offday_active_min"] if f["offday"] else CFG["offhours_active_min"])*60:
            onde = "fim de semana/feriado" if f["offday"] else "fora do horario comercial"
            S.append(sig(uid, d, "offhours", "media" if f["offhours_sec"] < 3*3600 else "alta",
                f"{hm(f['offhours_sec']/60)} ativo {onde}", "", f["offhours_sec"]/3600))
        # presente mas pouco ativo (expediente longo, pouca atividade)
        if not f["offday"] and f["jornada_min"] >= 8*60 and f["active_sec"]/60 < 0.27 * f["jornada_min"]:
            S.append(sig(uid, d, "hollow_day", "media",
                f"Expediente de {hm(f['jornada_min'])} ({clock(f['first_min'])}–{clock(f['last_min'])}) com só {hm(f['active_sec']/60)} ativo ({f['active_sec']/60/f['jornada_min']:.0%})", "",
                1 - f["active_sec"]/60/f["jornada_min"]))
        # sem pausa
        if f["jornada_min"] >= CFG["no_break_min_jornada_h"]*60 and f["longest_break_min"] < CFG["no_break_max_pause_min"]:
            S.append(sig(uid, d, "no_break", "baixa",
                f"Jornada de {hm(f['jornada_min'])} com maior pausa de so {hm(f['longest_break_min'])}", ""))
        # ferias
        if ucfg.get("on_vacation"):
            S.append(sig(uid, d, "vacation_activity", "media", f"Marcado de ferias, mas {hm(f['active_sec']/60)} ativo", ""))
    # periodo: sobrecarga
    long_days = [f for f in wd if f["active_sec"]/3600 >= 9.0]
    if len(long_days) >= CFG["overload_days"]:
        S.append(sig(uid, None, "overload", "baixa",
            f"{len(long_days)} dias com jornada muito longa no periodo (media ativa {hm(st.mean(f['active_sec'] for f in long_days)/60)})",
            ", ".join(f["day"].strftime("%d/%m") for f in long_days), len(long_days)))
    # tendencia
    if len(wd) >= 8:
        h = len(wd)//2
        a = st.mean(f["active_sec"] for f in wd[:h]); b = st.mean(f["active_sec"] for f in wd[h:])
        if a > 0 and (a-b)/a >= CFG["trend_drop"]:
            S.append(sig(uid, None, "engagement_drop", "media",
                f"Tempo ativo medio caiu de {hm(a/60)} para {hm(b/60)} por dia util ({(a-b)/a:.0%})", ""))
    return S

SEC_W = {"anonimizacao": 4, "pirataria": 4, "lista_tenant": 4, "compartilhamento": 3, "acesso_remoto": 3,
         "apostas": 3, "paste": 2, "ferramentas_ofensivas": 2, "email_pessoal": 1, "ia_externa": 1}
SEC_LBL = {"anonimizacao": "VPN/Tor/proxy", "pirataria": "Torrent/pirataria", "lista_tenant": "Lista de sites perigosos",
           "compartilhamento": "Compartilhamento de arquivos", "acesso_remoto": "Acesso remoto", "apostas": "Apostas",
           "paste": "Sites de paste", "ferramentas_ofensivas": "Ferramentas ofensivas", "email_pessoal": "E-mail pessoal",
           "ia_externa": "IA externa"}
SEC_DEPT_OK = re.compile(r"secur|seguran|infra|arquitetura", re.I)

def security_profile(fs, dept):
    tot = defaultdict(float); days = defaultdict(int); ex = defaultdict(list)
    for f in fs:
        for k, v in (f.get("sec") or {}).items():
            tot[k] += v.get("min", 0); days[k] += 1
            for e in v.get("ex", []):
                if e not in ex[k] and len(ex[k]) < 4: ex[k].append(e)
    score = 0.0
    for k, m in tot.items():
        w = SEC_W.get(k, 1)
        if k == "ferramentas_ofensivas" and SEC_DEPT_OK.search(dept or ""): w = 0.5
        # tempo pesa mais que recorrência: 1 min/dia em 10 dias não pode virar risco máximo
        score += w * (1 + min(m, 120) / 30) * (1 + 0.1 * min(days[k], 10))
    return min(100, round(score * 3)), tot, days, ex

def detect_team(per_user, users):
    """Comparacao com pares: fragmentacao, baixa atividade, ausencias e risco de seguranca."""
    S = []
    uinfo = {u["user_id"]: u for u in users}
    dept = lambda u: (uinfo.get(u, {}).get("department") or "Sem depto")
    avg = {}
    for u, fs in per_user.items():
        wd = [f for f in fs if f["worked"] and not f["offday"]]
        if len(wd) >= 4 and not uinfo.get(u, {}).get("on_vacation"):
            avg[u] = st.mean(f["active_sec"] for f in wd) / 60
    allmed = st.median(avg.values()) if avg else 0
    bydept = defaultdict(list)
    for u, a in avg.items(): bydept[dept(u)].append(a)
    REF = {}
    for u, a in avg.items():
        peers = bydept[dept(u)]
        ref = st.median(peers) if len(peers) >= 3 else allmed
        REF[u] = ref
        if ref and (a < 0.6 * ref or a < 180):
            sev = "alta" if (a < 0.45 * ref or a < 150) else "media"
            S.append(sig(u, None, "low_activity", sev,
                f"Média de {hm(a)} ativo por dia útil, {a/ref:.0%} da mediana do {'departamento' if len(peers) >= 3 else 'time'} ({hm(ref)})",
                f"departamento={dept(u)} n_pares={len(peers)}", 1 - a/ref))
    detect_team.REF = REF
    # ausencias: dias uteis sem atividade entre o 1o e o ultimo dia ativo
    for u, fs in per_user.items():
        if uinfo.get(u, {}).get("on_vacation"): continue
        act = [f["day"] for f in fs if f["worked"]]
        if len(act) < 3: continue
        lo, hi = min(act), max(act)
        miss = [f["day"] for f in fs if lo < f["day"] < hi and not f["offday"] and not f["worked"]]
        if miss:
            S.append(sig(u, None, "absent_days", "alta" if len(miss) >= 3 else "media",
                f"{len(miss)} dia(s) útil(eis) sem atividade no período",
                ", ".join(m.strftime("%d/%m") for m in miss), len(miss)))
    # WhatsApp: produtivo, mas aponta DESVIO em relação à própria pessoa
    for u, fs in per_user.items():
        wd = [f for f in fs if f["worked"] and not f["offday"]]
        wa = [(f, f["classes"].get("whatsapp", 0) / 60) for f in wd]
        if len(wa) < 4: continue
        med = st.median(m for _, m in wa)
        for f, m in wa:
            share = m * 60 / max(1, f["active_sec"])
            if m >= 60 and m >= 2 * max(med, 15) or (m >= 45 and share >= 0.40 and share >= 2 * med * 60 / max(1, f["active_sec"])):
                S.append(sig(u, f["day"], "whatsapp_deviation", "baixa",
                    f"{hm(m)} de WhatsApp ({share:.0%} do tempo ativo); o normal da pessoa é {hm(med)} por dia útil",
                    "", m / max(med, 15)))
        for f in fs:
            if not (f["worked"] and f["offday"]): continue
            m = f["classes"].get("whatsapp", 0) / 60
            if m >= 45:
                S.append(sig(u, f["day"], "whatsapp_deviation", "baixa",
                    f"{hm(m)} de WhatsApp em fim de semana/feriado", "", m / 45))
    # seguranca
    for u, fs in per_user.items():
        sc, tot, days, ex = security_profile(fs, dept(u))
        if sc <= 0: continue
        cats = sorted(tot, key=lambda k: -SEC_W.get(k, 1) * (1 + tot[k]))
        sev = "alta" if sc >= 50 else ("media" if sc >= 20 else "baixa")
        S.append(sig(u, None, "security_risk", sev,
            f"Índice {sc}/100: " + "; ".join(f"{SEC_LBL.get(k,k)} ({hm(tot[k])}, {days[k]} dia(s))" for k in cats[:4]),
            " | ".join(f"{SEC_LBL.get(k,k)}: " + " / ".join(ex[k][:2]) for k in cats[:3]), sc/20))
    vals = {u: [f["switch_per_h"] for f in fs if f["worked"]] for u, fs in per_user.items()}
    means = {u: st.mean(v) for u, v in vals.items() if len(v) >= 3}
    if len(means) >= 5:
        m = st.median(means.values()); md = mad(list(means.values())) * 1.4826 or 1
        for u, v in means.items():
            z = (v - m) / md
            if z >= 3:
                S.append(sig(u, None, "fragmentation", "baixa",
                    f"{v:.0f} trocas de app por hora ativa (mediana do time {m:.0f})", "", z))
    return S

# ----------------------------------------------------------------- L3 simulado
def resumo(name, feats, sigs):
    wd = [f for f in feats if f["worked"] and not f["offday"]]
    if not wd: return "Sem dias uteis com atividade suficiente no periodo."
    ini = med([f["first_min"] for f in wd]); fim = med([f["last_min"] for f in wd])
    act = st.mean(f["active_sec"] for f in wd)/60
    nw = sum(f["nonwork_sec"] for f in wd) / max(1, sum(f["active_sec"] for f in wd))
    p = [f"Padrao tipico: {clock(ini)} as {clock(fim)}, {hm(act)} ativo por dia util ({len(wd)} dias analisados)."]
    if nw >= 0.05: p.append(f"{nw:.0%} do tempo ativo foi em conteudo nao relacionado ao trabalho.")
    c = Counter(s["type"] for s in sigs)
    if not c: p.append("Nenhum sinal relevante no periodo: comportamento estavel.")
    else:
        top = ", ".join(f"{SIG_INFO[t][0].lower()} ({n}x)" for t, n in c.most_common(3))
        p.append(f"Pontos de atencao: {top}.")
        if c.get("overload") or c.get("no_break"):
            p.append("Vale conversar sobre carga de trabalho antes de tratar como problema de conduta.")
        if c.get("warmup_lag") or c.get("nonwork_share"):
            p.append("Confira a evidencia: categorizacao incompleta de sites/apps pode inflar esses sinais.")
    return " ".join(p)


def top_titles(wd, cls, n=4):
    c = Counter()
    for f in wd:
        for t, m in (f.get("tt") or {}).get(cls, []): c[t] += m
    return [[t, round(m)] for t, m in c.most_common(n) if m >= 1]

def extra_metrics(u, fs, dept):
    wd = [f for f in fs if f["worked"] and not f["offday"]]
    n = max(1, len(wd)); act = sum(f["active_sec"] for f in wd) or 1
    wa = sum(f["classes"].get("whatsapp", 0) for f in wd); so = sum(f["classes"].get("social", 0) for f in wd)
    sc, tot, days, ex = security_profile(fs, dept)
    cats = sorted(tot, key=lambda k: -SEC_W.get(k, 1) * (1 + tot[k]))
    ref = getattr(detect_team, "REF", {}).get(u)
    avg = (act / n / 60) if wd else 0
    return {"whatsapp_min_dia": round(wa / n / 60), "whatsapp_pct": round(100 * wa / act, 1),
            "social_min_dia": round(so / n / 60), "social_pct": round(100 * so / act, 1),
            "whatsapp_top": top_titles(wd, "whatsapp"), "social_top": top_titles(wd, "social"),
            "video_top": top_titles(wd, "video"),
            "risco": sc, "risco_categorias": "; ".join(f"{SEC_LBL.get(k,k)} ({round(tot[k])} min)" for k in cats),
            "risco_exemplos": " | ".join(f"{SEC_LBL.get(k,k)}: " + " / ".join(ex[k][:2]) for k in cats[:3]),
            "risco_detalhe": [{"categoria": SEC_LBL.get(k, k), "min": round(tot[k], 1), "dias": days[k], "ex": ex[k]} for k in cats],
            "engajamento": round(100 * avg / ref) if ref else None, "ref_depto_min": round(ref) if ref else None}

def export_data(tenant, users, per_user, sigs, period):
    uinfo = {u["user_id"]: u for u in users}
    people = []
    for u, fs in per_user.items():
        wd = [f for f in fs if f["worked"] and not f["offday"]]
        ss = [s for s in sigs if s["user_id"] == u]
        tot_act = sum(f["active_sec"] for f in fs if f["worked"])
        cls = Counter()
        for f in fs:
            if f["worked"]: cls.update(f["classes"])
        people.append({
            "id": u, "name": uinfo.get(u, {}).get("display_name") or u,
            "dept": uinfo.get(u, {}).get("department") or "Sem depto",
            "cargo": uinfo.get(u, {}).get("cargo"), "manager": uinfo.get(u, {}).get("manager"),
            "vacation": bool(uinfo.get(u, {}).get("on_vacation")),
            "days_worked": len([f for f in fs if f["worked"]]),
            "avg_active_min": round(st.mean(f["active_sec"] for f in wd)/60) if wd else 0,
            "med_start": clock(med([f["first_min"] for f in wd])) if wd else None,
            "med_end": clock(med([f["last_min"] for f in wd])) if wd else None,
            "nonwork_pct": round(100*sum(f["nonwork_sec"] for f in fs if f["worked"])/tot_act, 1) if tot_act else 0,
            "offhours_min": round(sum(f["offhours_sec"] for f in fs if f["worked"])/60),
            "night_min": round(sum(f["night_sec"] for f in fs if f["worked"])/60),
            "switch_h": round(st.mean(f["switch_per_h"] for f in wd), 1) if wd else 0,
            "classes_min": {k: round(v/60) for k, v in cls.items()},
            "summary": resumo(u, fs, ss),
            **extra_metrics(u, fs, uinfo.get(u, {}).get("department") or "Sem depto"),
            "days": [{"d": f["day"].isoformat(), "off": f["offday"], "w": f["worked"],
                      "act": round(f["active_sec"]/60),
                      "start": clock(f.get("first_min")) if f["worked"] else None,
                      "eff": clock(f.get("effective_min")) if f["worked"] else None,
                      "end": clock(f.get("last_min")) if f["worked"] else None,
                      "nw": round(f.get("nonwork_sec", 0)/60),
                      "raw": clock(f.get("raw_first")) if f["worked"] and f.get("raw_first") is not None else None,
                      "brk": round(f.get("longest_break_min", 0)) if f["worked"] else None,
                      "night": round(f.get("night_sec", 0)/60), "offm": round(f.get("offhours_sec", 0)/60),
                      "afk_in": round(f.get("afk_in_sec", 0)/60),
                      "runs": (f.get("runs") or [])[:3], "apps": (f.get("top_apps") or [])[:6],
                      "cls": {k: round(v/60) for k, v in (f.get("classes") or {}).items()},
                      "h": [round(x/60) for x in f.get("by_hour", [0]*24)],
                      "hn": [round(x/60) for x in f.get("nw_by_hour", [0]*24)]} for f in fs],
        })
    data = {"tenant": tenant, "since": period[0].isoformat(), "until": period[1].isoformat(),
            "biz": [CFG["biz_start"], CFG["biz_end"]],
            "signal_types": {k: {"label": v[0], "desc": v[1]} for k, v in SIG_INFO.items()},
            "people": people,
            "signals": [{**s, "label": SIG_INFO[s["type"]][0]} for s in sigs]}
    return data

def feats_from_api(day, u):
    """Converte o formato de /v1/behavior/day (minutos) p/ o dict de features."""
    f = {"day": day, "active_sec": u.get("act", 0)*60, "offday": is_offday(day)}
    if f["active_sec"] < CFG["min_day_active_min"]*60 or "f" not in u:
        f["worked"] = False; return f
    st0 = u.get("s", u["f"])
    f.update({"worked": True, "first_min": st0, "raw_first": u["f"], "last_min": u["l"], "jornada_min": u["l"]-st0,
              "afk_in_sec": u.get("ai", 0)*60, "effective_min": u.get("eff"),
              "by_hour": [x*60 for x in u["h"]], "nw_by_hour": [x*60 for x in u["hn"]],
              "nonwork_sec": u.get("nw", 0)*60, "classes": {k: v*60 for k, v in u.get("cls", {}).items()},
              "cats": {k: v*60 for k, v in u.get("cat", {}).items()}, "top_apps": u.get("apps", []),
              "switch_per_h": u.get("sw", 0), "longest_break_min": u.get("brk", 0),
              "afk_by_hour": [x*60 for x in u.get("ha", [0]*24)], "tt": u.get("tt") or {}})
    f["night_sec"] = sum(f["by_hour"][CFG["night_start"]:CFG["night_end"]])
    f["offhours_sec"] = f["active_sec"] if f["offday"] else (sum(f["by_hour"][:CFG["biz_start"]]) + sum(f["by_hour"][CFG["biz_end"]:]))
    runs = []
    for r in u.get("runs") or []:
        c = classify(r["app"], r["t"]) or r.get("c", "")
        runs.append({"min": r["min"], "app": r["app"], "title": r["t"], "at": r["at"], "cls": c})
    cand = [r for r in runs if r["cls"] != "reuniao"]
    f["longest_run"] = cand[0] if cand else None
    f["runs"] = u.get("runs", [])
    f["meeting_sec"] = f["classes"].get("reuniao", 0)
    f["sec"] = u.get("sec") or {}
    return f

# ----------------------------------------------------------------- main


DOW_PT = ["Seg", "Ter", "Qua", "Qui", "Sex", "Sáb", "Dom"]

def hybrid_profile(fs, office_days):
    """Dias úteis trabalhados: média de tempo ativo por dia da semana e por modalidade (presencial x casa).
    office_days = dias da semana presenciais (0=seg ... 4=sex)."""
    wd = [f for f in fs if f["worked"] and not f["offday"]]
    by = defaultdict(list)
    for f in wd: by[f["day"].weekday()].append(f["active_sec"] / 60)
    of = [m for d, v in by.items() if d in office_days for m in v]
    ho = [m for d, v in by.items() if d not in office_days and d < 5 for m in v]
    o = {"dow": {DOW_PT[d]: round(st.mean(v)) for d, v in sorted(by.items()) if d < 5},
         "dow_n": {DOW_PT[d]: len(v) for d, v in sorted(by.items()) if d < 5},
         "esc_min": round(st.mean(of)) if of else None, "casa_min": round(st.mean(ho)) if ho else None,
         "dias_esc": len(of), "dias_casa": len(ho), "dif_pct": None, "menos_ativo": None}
    if of and ho and o["esc_min"]:
        o["dif_pct"] = round(100 * (o["casa_min"] - o["esc_min"]) / o["esc_min"])
    if o["dow"]:
        o["menos_ativo"] = min(o["dow"], key=lambda k: o["dow"][k])
    return o

def detect_hybrid(per_user, office_days):
    """Sinal SUTIL (baixa): só quando a diferença é grande, consistente e com dias suficientes dos dois lados."""
    S = []
    for u, fs in per_user.items():
        h = hybrid_profile(fs, office_days)
        if h["dias_esc"] < 3 or h["dias_casa"] < 2 or h["dif_pct"] is None: continue
        diff = h["casa_min"] - h["esc_min"]
        if abs(h["dif_pct"]) >= 30 and abs(diff) >= 60:
            onde = "em casa" if diff < 0 else "no escritório"
            S.append(sig(u, None, "hybrid_gap", "baixa",
                f"Dias menos ativos: {onde}. Média de {hm(h['casa_min'])} em casa ({h['dias_casa']} dias) e "
                f"{hm(h['esc_min'])} no escritório ({h['dias_esc']} dias)",
                "Pode ter motivo legítimo (reuniões presenciais, deslocamento, tipo de tarefa). Vale entender antes de concluir.",
                abs(h["dif_pct"]) / 30))
    return S

def analyze(tenant, people, day_payloads, office_days=None):
    """people: lista de /v1/behavior/people; day_payloads: {"AAAA-MM-DD": payload de /v1/behavior/day}."""
    users = [{"user_id": p["user_id"], "display_name": p.get("display_name") or p["user_id"],
              "department": p.get("department"), "on_vacation": p.get("on_vacation"),
              "cargo": p.get("cargo"), "manager": p.get("manager")} for p in people]
    per_user = defaultdict(list); all_days = set(); partial = {}
    for ds in sorted(day_payloads):
        j = day_payloads[ds]; day = date.fromisoformat(ds); all_days.add(day); partial[day] = j.get("partial")
        for uid, u in (j.get("users") or {}).items():
            per_user[uid].append(feats_from_api(day, u))
    for uid in list(per_user):
        have = {f["day"] for f in per_user[uid]}
        for d in all_days - have: per_user[uid].append({"day": d, "active_sec": 0, "offday": is_offday(d), "worked": False})
        per_user[uid].sort(key=lambda f: f["day"])
        if not any(f["worked"] for f in per_user[uid]): del per_user[uid]
    if not all_days:
        return {"tenant": tenant, "since": None, "until": None, "people": [], "signals": []}
    sigs = []
    uinfo = {u["user_id"]: u for u in users}
    for u, fs in per_user.items():
        full = [f for f in fs if not partial.get(f["day"])]   # hoje (parcial) não entra nos sinais de fim de dia
        sigs += detect_user(u, full, uinfo.get(u, {}))
    sigs += detect_team(per_user, users)
    office = set(office_days if office_days is not None else CFG.get("office_days", (0, 2, 4)))
    sigs += detect_hybrid(per_user, office)
    data = export_data(tenant, users, per_user, sigs, (min(all_days), max(all_days)))
    for p in data["people"]:
        p["hybrid"] = hybrid_profile(per_user[p["id"]], office)
    data["office_days"] = sorted(office)
    return data


# ----------------------------------------------------------------- pacote do painel
FIX = [(r"\bInicio\b","Início"),(r"\bHorario\b","Horário"),(r"\bhorario\b","horário"),(r"\bpadrao\b","padrão"),(r"\bPadrao\b","Padrão"),
       (r"\bComeca\b","Começa"),(r"\bMaquina\b","Máquina"),(r"\bso as\b","só às"),(r"\bso\b","só"),(r" as (\d\d:\d\d)",r" às \1"),
       (r"\bnao\b","não"),(r"\buteis\b","úteis"),(r"\butil\b","útil"),(r"\bperiodo\b","período"),(r"\btipico\b","típico"),
       (r"\batencao\b","atenção"),(r"\bcategorizacao\b","categorização"),(r"\bevidencia\b","evidência"),(r"\bestavel\b","estável"),
       (r"\bsessao\b","sessão"),(r"\bferias\b","férias"),(r"\bVarios\b","Vários"),(r"\bmedia ativa\b","média ativa"),
       (r"\bmedio\b","médio"),(r"\btitulo\b","título"),(r"\bconteudo\b","conteúdo"),(r"\bmadrugada\b","madrugada"),
       (r"\bvideo\b","vídeo"),(r"\bnoticia\b","notícia"),(r"\bseguidos em\b","seguidos em"),(r"\bsabados\b","sábados"),
       (r"\bsegundas\b","segundas"),(r"\btercas\b","terças"),(r"\bjornadas?\b",lambda m:m.group(0)),(r"\bespecifico\b","específico")]
def fx(s):
    if not s: return s
    for a, b in FIX: s = re.sub(a, b, s)
    return s
LBL = {"schedule_deviation":"Horário fora do padrão","warmup_lag":"Começa tarde após ligar","night_work":"Atividade de madrugada",
       "long_static":"Muito tempo parado numa janela","nonwork_share":"Uso não relacionado ao trabalho","offhours":"Trabalho fora do horário",
       "overload":"Sobrecarga sustentada","no_break":"Sem pausa","fragmentation":"Muita troca de contexto","engagement_drop":"Queda de engajamento",
       "vacation_activity":"Ativo durante férias","social_media":"Uso de redes sociais","low_activity":"Baixa atividade",
       "hollow_day":"Presente mas pouco ativo","absent_days":"Dias úteis sem atividade","security_risk":"Risco de segurança","whatsapp_deviation":"WhatsApp fora do padrão","hybrid_gap":"Ritmo diferente casa × escritório"}
LBL["nonwork_share"]="Vídeo, notícias, compras ou jogos"


def build_pack(d):
    """Dashboard (analyze) -> as 4 tabelas do painel, prontas para o navegador."""
    P = {p["id"]: p for p in d["people"]}
    sig = []
    for s in d["signals"]:
        p = P.get(s["user_id"], {})
        sig.append({"user_id": s["user_id"], "nome": p.get("name"), "depto": p.get("dept"), "dia": s["day"] or "período",
                    "tipo": s["type"], "sinal": LBL[s["type"]], "severidade": s["sev"], "descricao": fx(s["msg"]),
                    "evidencia": fx(s["evid"]), "score": s["score"]})
    per = defaultdict(Counter); pert = defaultdict(Counter)
    for s in sig: per[s["user_id"]][s["severidade"]] += 1; pert[s["user_id"]][s["sinal"]] += 1
    people = []
    for p in d["people"]:
        c = per[p["id"]]
        people.append({"user_id": p["id"], "nome": p["name"], "depto": p["dept"], "cargo": p.get("cargo") or "", "gestor": p.get("manager") or "",
            "ferias": p.get("vacation", False), "dias_trabalhados": p["days_worked"], "ativo_medio_min": p["avg_active_min"],
            "inicio_mediano": p["med_start"], "fim_mediano": p["med_end"], "nao_trabalho_pct": p["nonwork_pct"],
            "fora_horario_min": p["offhours_min"], "madrugada_min": p["night_min"], "trocas_por_h": p["switch_h"],
            "sinais_alta": c["alta"], "sinais_media": c["media"], "sinais_baixa": c["baixa"],
            "pontos": 3*c["alta"] + 2*c["media"] + c["baixa"],
            "principais_sinais": "; ".join(f"{k} ({n})" for k, n in pert[p["id"]].most_common(3)),
            "resumo": fx(p["summary"]),
            "whatsapp_min_dia": p.get("whatsapp_min_dia", 0), "whatsapp_pct": p.get("whatsapp_pct", 0),
            "social_min_dia": p.get("social_min_dia", 0), "social_pct": p.get("social_pct", 0),
            "hib_escritorio_min": (p.get("hybrid") or {}).get("esc_min"), "hib_casa_min": (p.get("hybrid") or {}).get("casa_min"),
        "hib_dias_escritorio": (p.get("hybrid") or {}).get("dias_esc", 0), "hib_dias_casa": (p.get("hybrid") or {}).get("dias_casa", 0),
        "hib_dif_pct": (p.get("hybrid") or {}).get("dif_pct"), "hib_menos_ativo": (p.get("hybrid") or {}).get("menos_ativo"),
        "hib_por_dia": ",".join(f"{k}:{v}" for k, v in ((p.get("hybrid") or {}).get("dow") or {}).items()),
        "whatsapp_sites": " · ".join(f"{fx(t)} ({m} min)" for t, m in p.get("whatsapp_top", [])),
            "social_sites": " · ".join(f"{fx(t)} ({m} min)" for t, m in p.get("social_top", [])),
            "video_sites": " · ".join(f"{fx(t)} ({m} min)" for t, m in p.get("video_top", [])),
            "risco": p.get("risco", 0), "risco_categorias": p.get("risco_categorias", ""), "risco_exemplos": p.get("risco_exemplos", ""),
            "engajamento": p.get("engajamento"), "ref_depto_min": p.get("ref_depto_min")})
    days = []
    for p in d["people"]:
        for x in p["days"]:
            days.append({"user_id": p["id"], "dia": x["d"], "fim_de_semana_feriado": x["off"], "trabalhou": x["w"], "ativo_min": x["act"],
                "inicio": x.get("start"), "primeira_atividade": x.get("raw"), "inicio_efetivo": x.get("eff"), "fim": x.get("end"),
                "nao_trabalho_min": x.get("nw", 0), "maior_pausa_min": x.get("brk"), "madrugada_min": x.get("night", 0),
                "fora_horario_min": x.get("offm", 0), "afk_jornada_min": x.get("afk_in", 0),
                "por_hora": ",".join(str(v) for v in x["h"]), "nao_trabalho_por_hora": ",".join(str(v) for v in x["hn"]),
                "top_apps": "; ".join(f"{a} {m}min" for a, m in (x.get("apps") or [])[:5]),
                "maior_bloco": (lambda r: f"{r['app']} — {r['min']} min às {r['at']//60:02d}:{r['at']%60:02d} ({fx(r.get('t') or r.get('title') or '')[:60]})" if r else "")((x.get("runs") or [None])[0])})
    meta = {"periodo": [d["since"], d["until"]]}
    seg = []
    for p in d["people"]:
        for r in p.get("risco_detalhe", []):
            seg.append({"user_id": p["id"], "nome": p["name"], "depto": p["dept"], "categoria": r["categoria"],
                        "minutos": r["min"], "dias": r["dias"], "exemplos": " / ".join(r["ex"])})

    return {"meta": {"tenant": d["tenant"], "periodo": [d["since"], d["until"]], "pessoas": len(people), "sinais": len(sig), "dias_presenciais": [DOW_PT[i] for i in d.get("office_days", [])]},
            "pessoas": people, "sinais": sig, "dias": days, "seguranca": seg}
