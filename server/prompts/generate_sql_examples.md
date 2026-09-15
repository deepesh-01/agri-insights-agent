---
id: generate_sql_examples
version: 8
purpose: >
  Few-shot examples for generate_sql. Every example exists to teach one trap or
  one query shape that a small model gets wrong zero-shot.

  Literal values are deliberately varied. An earlier version used season='rabi'
  four times and district='Mysore' four times, and the model copied both into a
  question that asked for neither - turning a count of 387 into 93. The
  examples must teach patterns, not values, so no district, season or year
  appears more than once.
---
Q: How many farmers are registered in Belgaum?
A: {"action":"sql","sql":"SELECT count(*) AS farmer_count FROM farmer WHERE district = 'Belgaum'","assumptions":["counted farmers by their registered district"],"confidence":0.95}

Q: Which crops had the worst yield against expectation in the kharif season?
A: {"action":"sql","sql":"SELECT c.crop, avg(c.actual_yield / p.area_hectares / c.expected_yield) AS pct_of_expected, count(*) AS cycles FROM crop_cycle c JOIN plot p ON p.id = c.plot_id WHERE c.season = 'kharif' AND c.actual_yield IS NOT NULL GROUP BY c.crop ORDER BY pct_of_expected ASC","assumptions":["actual_yield is total kg so it is divided by area_hectares before comparing with expected_yield in kg/ha","harvested cycles only; 153 cycles have no recorded yield"],"confidence":0.85}

Q: What is the average soil moisture in Dharwad?
A: {"action":"sql","sql":"SELECT avg(s.value) AS avg_soil_moisture, count(DISTINCT s.plot_id) AS plots FROM sensor_reading s JOIN plot p ON p.id = s.plot_id WHERE p.district = 'Dharwad' AND s.reading_type = 'soil_moisture' AND s.value NOT IN (-273, -99, 500, 999.9)","assumptions":["sentinel error values excluded","plot district, not farmer district","covers only instrumented plots; 202 of 450 plots have sensors"],"confidence":0.9}

Q: Total rainfall recorded per district
A: {"action":"sql","sql":"WITH clean AS (SELECT DISTINCT ON (s.plot_id, s.recorded_at) s.plot_id, s.value FROM sensor_reading s WHERE s.reading_type = 'rainfall' AND s.value NOT IN (-273, -99, 500, 999.9) ORDER BY s.plot_id, s.recorded_at, s.id) SELECT p.district, round(sum(c.value), 1) AS total_rainfall_mm FROM clean c JOIN plot p ON p.id = c.plot_id GROUP BY p.district ORDER BY total_rainfall_mm DESC","assumptions":["duplicate readings de-duplicated before summing","sentinel values excluded","instrumented plots only"],"confidence":0.8}

Q: How many visits did each agent make in 2026?
A: {"action":"sql","sql":"SELECT a.name, count(*) AS visits FROM field_visit v JOIN field_agent a ON a.id = v.agent_id WHERE v.visited_at >= DATE '2026-01-01' AND v.visited_at < DATE '2027-01-01' AND v.outcome IN ('completed', 'partial') GROUP BY a.name ORDER BY visits DESC","assumptions":["counted visits that actually happened (completed or partial); cancelled and farmer_absent excluded"],"confidence":0.85}

Previous SQL: SELECT c.crop, avg(c.actual_yield / p.area_hectares) AS yield_per_ha FROM crop_cycle c JOIN plot p ON p.id = c.plot_id WHERE p.district = 'Tumkur' AND c.season = 'kharif' AND c.sown_date >= DATE '2025-01-01' AND c.sown_date < DATE '2026-01-01' AND c.actual_yield IS NOT NULL GROUP BY c.crop
Q: no, I meant rabi, not kharif
A: {"action":"sql","sql":"SELECT c.crop, avg(c.actual_yield / p.area_hectares) AS yield_per_ha FROM crop_cycle c JOIN plot p ON p.id = c.plot_id WHERE p.district = 'Tumkur' AND c.season = 'rabi' AND c.sown_date >= DATE '2025-01-01' AND c.sown_date < DATE '2026-01-01' AND c.actual_yield IS NOT NULL GROUP BY c.crop","assumptions":["season changed to rabi; district, year and grouping kept"],"confidence":0.9}

Previous SQL: SELECT c.crop, avg(c.actual_yield / p.area_hectares) AS yield_per_ha FROM crop_cycle c JOIN plot p ON p.id = c.plot_id WHERE p.district = 'Hassan' AND c.season = 'summer' AND c.actual_yield IS NOT NULL GROUP BY c.crop
Q: only irrigated plots
A: {"action":"sql","sql":"SELECT c.crop, avg(c.actual_yield / p.area_hectares) AS yield_per_ha FROM crop_cycle c JOIN plot p ON p.id = c.plot_id WHERE p.district = 'Hassan' AND c.season = 'summer' AND c.actual_yield IS NOT NULL AND p.irrigation_type <> 'rainfed' GROUP BY c.crop","assumptions":["irrigated means irrigation_type is not rainfed","everything else kept from the previous query"],"confidence":0.9}

Q: For each district, which crop has the highest yield per hectare?
A: {"action":"sql","sql":"WITH per_crop AS (SELECT p.district, c.crop, avg(c.actual_yield / p.area_hectares) AS yield_per_ha, rank() OVER (PARTITION BY p.district ORDER BY avg(c.actual_yield / p.area_hectares) DESC) AS rk FROM crop_cycle c JOIN plot p ON p.id = c.plot_id WHERE c.actual_yield IS NOT NULL GROUP BY p.district, c.crop) SELECT district, crop, yield_per_ha FROM per_crop WHERE rk = 1","assumptions":["ranked crops within each district and kept the top one","harvested cycles only"],"confidence":0.8}

Q: Which farmers have the highest credit score?
A: {"action":"refuse","reason":"There is no credit score in this database. It holds farmers, plots, crop cycles, sensor readings, advisories, field agents and field visits - no financial data of any kind.","confidence":0.95}

Q: What is the average yield in Belgaum?
A: {"action":"clarify","question":"Do you mean plots located in Belgaum, or farmers registered in Belgaum? They differ for about one plot in five, and the two give noticeably different averages.","confidence":0.9}
