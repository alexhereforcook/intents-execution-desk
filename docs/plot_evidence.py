"""Rebuild docs/evidence_series.png: python docs/plot_evidence.py docs/evidence_series_orders.csv docs/evidence_series.png"""
import sys, pandas as pd, numpy as np, matplotlib
matplotlib.use("Agg"); import matplotlib.pyplot as plt
d=pd.read_csv(sys.argv[1]); out=sys.argv[2]
num=lambda s: pd.to_numeric(s.astype(str).str.replace(",",""),errors="coerce")
for c in d.columns[4:]:
    if c!="sold": d[c]=num(d[c])
s=d[d.source.str.startswith("series")].copy()
TOK=[("RHEA","#2a78d6","o"),("AURORA","#eb6834","s"),("wNEAR","#1baf7a","^")]
INK,MUTED,GRID,BG="#0b0b0b","#52514e","#e4e3df","#fcfcfb"
plt.rcParams.update({"font.family":"DejaVu Sans","font.size":10.5,"axes.edgecolor":GRID,"axes.labelcolor":MUTED,
  "xtick.color":MUTED,"ytick.color":MUTED,"axes.spines.top":False,"axes.spines.right":False})
fig,ax=plt.subplots(1,3,figsize=(17,5.4),facecolor=BG)
for a in ax: a.set_facecolor(BG); a.grid(color=GRID,lw=0.8); a.set_axisbelow(True)
# 1 execution forecast vs actual
a=ax[0]; lim=160; a.plot([0,lim],[0,lim],color=MUTED,lw=1,ls=(0,(4,4)))
for t,c,m in TOK:
    x=s[s.token==t]; a.scatter(x["forecast execution, bps"],x["execution, bps"],s=34,color=c,marker=m,edgecolor=BG,lw=1,label=t,alpha=.9)
err=(s["execution, bps"]-s["forecast execution, bps"]).abs().median()
a.set(xlim=(0,lim),ylim=(0,lim),xlabel="forecast at order start, bps",ylabel="actual, bps")
a.set_title(f"Execution cost: forecast vs actual\n{len(s)} orders, median error {err:.0f} bps",loc="left",color=INK,fontsize=12)
a.text(lim*0.62,lim*0.66,"forecast = actual",color=MUTED,rotation=45,fontsize=9)
a.legend(frameon=False,loc="upper left")
# 2 timing risk calibration
a=ax[1]; lim=190; a.plot([0,lim],[0,lim],color=MUTED,lw=1,ls=(0,(4,4)))
g=s.groupby(["token","horizon, h"])
cell=pd.DataFrame({"fc":g["forecast timing risk ±, bps"].mean(),"rms":g["timing, bps"].apply(lambda v:np.sqrt((v**2).mean()))}).reset_index()
for t,c,m in TOK:
    x=cell[cell.token==t].sort_values("horizon, h")
    a.plot(x.fc,x.rms,color=c,lw=2,marker=m,ms=8,mec=BG,mew=1.5,label=t)
    for _,r in x.iterrows(): a.annotate(f"{r['horizon, h']:g}h",(r.fc,r.rms),textcoords="offset points",xytext=(6,-4),fontsize=8.5,color=MUTED)
a.set(xlim=(0,lim),ylim=(0,lim),xlabel="forecast timing risk (1σ), bps",ylabel="realised timing, RMS bps")
a.set_title("Timing risk: forecast vs realised\nabove the line = market moved more than forecast",loc="left",color=INK,fontsize=12)
a.legend(frameon=False,loc="upper left")
# 3 IS by horizon
a=ax[2]; H=[0.5,1,2,4]
for t,c,m in TOK:
    x=s[s.token==t].groupby("horizon, h")["IS, bps"].mean().reindex(H)
    b=int(np.nanargmin(x.values))
    a.plot(range(4),x.values,color=c,lw=2,marker=m,ms=8,mec=BG,mew=1.5,label=f"{t}: best {H[b]:g} h, {x.values[b]:.0f} bps")
    a.scatter([b],[x.values[b]],s=260,facecolor="none",edgecolor=c,lw=1.5)
a.set_xticks(range(4),[f"{h:g} h" for h in H]); a.set_ylim(0,150); a.set_xlim(-0.3,3.3)
a.set(xlabel="time to sell",ylabel="implementation shortfall, avg bps")
a.set_title("Total cost by horizon (circle = cheapest)\none swap: AURORA ~850 bps, RHEA impossible",loc="left",color=INK,fontsize=12)
a.legend(frameon=False,loc="lower right",fontsize=9.5)
fig.tight_layout(w_pad=3); fig.savefig(out,dpi=150,facecolor=BG)
