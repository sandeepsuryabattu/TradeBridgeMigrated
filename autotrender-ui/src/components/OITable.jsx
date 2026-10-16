export default function OITable({ rows }) {
  const fmt = (n) => Number(n).toLocaleString("en-IN");

  return (
    <div className="overflow-x-auto rounded-xl border border-white/10">
      <table className="w-full text-[12px] tabular-nums">
        <thead>
          <tr className="text-neutral-400">
            <th className="px-3 py-2 text-right font-medium">Call OI</th>
            <th className="px-3 py-2 text-right font-medium">Chg OI</th>
            <th className="px-3 py-2 text-right font-medium">Call LTP</th>
            <th className="px-3 py-2 text-center font-medium">Strike</th>
            <th className="px-3 py-2 text-right font-medium">Put LTP</th>
            <th className="px-3 py-2 text-right font-medium">Put OI</th>
            <th className="px-3 py-2 text-right font-medium">Chg OI</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr
              key={row.strike}
              className={row.atm ? "bg-emerald-500/10" : ""}
            >
              <td className="px-3 py-2 text-right">{fmt(row.callOI)}</td>
              <td className="px-3 py-2 text-right">{fmt(row.callChgOI)}</td>
              <td className="px-3 py-2 text-right">{fmt(row.callLTP)}</td>
              <td
                className={`px-3 py-2 text-center ${
                  row.atm ? "font-bold" : "font-medium"
                }`}
              >
                {fmt(row.strike)}
              </td>
              <td className="px-3 py-2 text-right">{fmt(row.putLTP)}</td>
              <td className="px-3 py-2 text-right">{fmt(row.putOI)}</td>
              <td className="px-3 py-2 text-right">{fmt(row.putChgOI)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
