// Pure position-size math over user-entered inputs. No market data.
export function calcPosition(capital, riskPct, entry, stop) {
  const cap = Number(capital) || 0;
  const risk = Number(riskPct) || 0;
  const ent = Number(entry) || 0;
  const stp = Number(stop) || 0;
  const riskAmt = (cap * risk) / 100;
  const perUnit = Math.abs(ent - stp);
  const qty = perUnit > 0 ? Math.floor(riskAmt / perUnit) : 0;
  const positionValue = qty * ent;
  return { riskAmt, qty, positionValue };
}
