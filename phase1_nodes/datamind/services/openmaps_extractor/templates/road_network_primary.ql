[out:json][timeout:{{timeout_s}}];
(
  way({{bbox}})["highway"~"primary|secondary|tertiary"];
);
out body;
