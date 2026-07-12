-- 0002_costbench_results: CostBench multi-turn cost-suite tables (wq#97, proposal cb8adf757b1d)
CREATE TABLE IF NOT EXISTS costbench_runs (
  run_id TEXT PRIMARY KEY,
  ts TEXT NOT NULL,
  model TEXT NOT NULL,
  blocker_mode TEXT,
  refinement_level INTEGER,
  total_queries INTEGER,
  valid_num INTEGER,
  final_answer_accuracy REAL,
  cost_ratio REAL,
  mean_per_query_ratio REAL,
  tool_path_exact_match_ratio REAL,
  total_tool_calls INTEGER,
  elapsed_s REAL,
  results_path TEXT,
  notes TEXT
);
CREATE TABLE IF NOT EXISTS costbench_results (
  run_id TEXT NOT NULL,
  query_id TEXT NOT NULL,
  task TEXT,
  turn_steps INTEGER,
  success INTEGER,
  goal_state INTEGER,
  status TEXT,
  sim_success_ratio REAL,
  PRIMARY KEY (run_id, query_id)
);
CREATE INDEX IF NOT EXISTS idx_costbench_results_run ON costbench_results(run_id);
