[out:json][timeout:{{timeout_s}}];
(
  relation({{bbox}})["type"="route"]["route"="bus"];
);
out tags;
