import { TrendingUp } from "lucide-react";

export default function Header({ nifty = 0, vix = null, expiry, live = false }) {
  const price = Number(nifty) || 0;
  const vixNum = Number(vix);
  // ISO YYYY-MM-DD → DD-MM-YYYY; anything else passes through untouched.
  const expiryShown = (() => {
    const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(expiry ?? ""));
    return m ? `${m[3]}-${m[2]}-${m[1]}` : expiry;
  })();

  return (
    <header className="sticky top-0 z-50 bg-neutral-900 border-b border-white/10 px-4 py-3">
      <div className="flex items-center justify-between gap-4">
        {/* Left: brand */}
        <div className="flex items-center gap-2 min-w-0">
          <span className="flex h-8 w-8 items-center justify-center rounded-lg bg-emerald-500/15 text-emerald-400">
            <TrendingUp size={18} strokeWidth={2.5} />
          </span>
          <span className="truncate text-sm font-semibold tracking-tight text-white">
            Autotrender Personal
          </span>
          <span className="rounded-full border border-white/10 bg-white/5 px-2 py-0.5 text-[10px] font-bold tracking-widest text-neutral-300">
            PERSONAL
          </span>
        </div>

        {/* Center: spot + vix */}
        <div className="flex items-center gap-6">
          <div className="flex items-baseline gap-2">
            <span className="text-[11px] font-medium tracking-wider text-neutral-400">
              NIFTY 50
            </span>
            <span className="text-sm font-semibold tabular-nums text-white">
              {price > 0
                ? price.toLocaleString("en-IN", {
                    minimumFractionDigits: 2,
                    maximumFractionDigits: 2,
                  })
                : "—"}
            </span>
          </div>
          <div className="hidden sm:flex items-baseline gap-2">
            <span className="text-[11px] font-medium tracking-wider text-neutral-400">
              INDIA VIX
            </span>
            <span className="text-sm font-semibold tabular-nums text-white">
              {Number.isFinite(vixNum) && vixNum > 0 ? vixNum.toFixed(2) : "—"}
            </span>
          </div>
        </div>

        {/* Right: expiry + feed badge */}
        <div className="flex items-center gap-2">
          <span className="rounded-md border border-white/10 bg-neutral-800 px-2 py-1.5 text-xs font-medium text-neutral-200">
            <span className="text-neutral-500 mr-1">Expiry</span>{expiryShown}
          </span>
          {live ? (
            <span className="flex items-center gap-1.5 rounded-full border border-emerald-400/20 bg-emerald-400/10 px-2.5 py-1 text-[10px] font-bold tracking-widest text-emerald-300">
              <span className="h-1.5 w-1.5 rounded-full bg-emerald-300" />
              LIVE
            </span>
          ) : (
            <span className="flex items-center gap-1.5 rounded-full border border-amber-400/20 bg-amber-400/10 px-2.5 py-1 text-[10px] font-bold tracking-widest text-amber-300">
              <span className="h-1.5 w-1.5 rounded-full bg-amber-300" />
              OFFLINE
            </span>
          )}
        </div>
      </div>
    </header>
  );
}
