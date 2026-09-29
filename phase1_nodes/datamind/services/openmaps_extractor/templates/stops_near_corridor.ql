[out:json][timeout:{{timeout_s}}];
(
  node({{bbox}})["highway"="bus_stop"];
);
out body;
