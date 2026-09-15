import { useState } from "react";
import type { SchemaResponse } from "../types";

/**
 * The data stops before today, so the boundary is stated before the first
 * question rather than discovered through an empty result.
 */
export function CoverageBanner({ schema }: { schema: SchemaResponse }) {
  const [open, setOpen] = useState(false);
  const sensor = schema.data_coverage["sensor_reading.recorded_at"];

  return (
    <div className="coverage">
      <div className="coverage-head">
        <strong>This data is historical.</strong>{" "}
        The latest record is {schema.latest_observation_date}
        {sensor && <> and sensor readings stop on {sensor.max.slice(0, 10)}</>}.
        Questions about later periods will correctly return nothing.
        <button className="link" onClick={() => setOpen((v) => !v)}>
          {open ? "hide details" : "per-table coverage"}
        </button>
      </div>
      {open && (
        <table className="coverage-table">
          <thead>
            <tr><th>Column</th><th>From</th><th>To</th><th /></tr>
          </thead>
          <tbody>
            {Object.entries(schema.data_coverage).map(([column, span]) => (
              <tr key={column}>
                <td><code>{column}</code></td>
                <td>{span.min.slice(0, 10)}</td>
                <td>{span.max.slice(0, 10)}</td>
                <td>
                  {span.forward_looking && (
                    <span className="tag tag-note">
                      {span.rows_in_future} future-dated rows
                    </span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
