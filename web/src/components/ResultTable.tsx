import { useState } from "react";
import type { Column } from "../types";

const PAGE = 25;

export function ResultTable({
  columns,
  rows,
}: {
  columns: Column[];
  rows: (string | number | null)[][];
}) {
  const [limit, setLimit] = useState(PAGE);
  const visible = rows.slice(0, limit);

  return (
    <div className="result">
      <div className="result-scroll">
        <table>
          <thead>
            <tr>
              {columns.map((column) => (
                <th key={column.name}>
                  {column.name}
                  <span className="coltype">{column.type}</span>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {visible.map((row, index) => (
              <tr key={index}>
                {row.map((cell, cellIndex) => (
                  <td key={cellIndex} className={cell === null ? "null" : ""}>
                    {cell === null ? "NULL" : format(cell)}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="result-foot muted small">
        {rows.length} {rows.length === 1 ? "row" : "rows"}
        {limit < rows.length && (
          <button className="link" onClick={() => setLimit((v) => v + PAGE)}>
            show {Math.min(PAGE, rows.length - limit)} more
          </button>
        )}
      </div>
    </div>
  );
}

function format(value: string | number): string {
  if (typeof value !== "number") return value;
  if (Number.isInteger(value)) return value.toLocaleString();
  return value.toLocaleString(undefined, { maximumFractionDigits: 2 });
}
