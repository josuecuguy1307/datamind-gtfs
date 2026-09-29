[out:json][timeout:{{timeout_s}}];
(
  node({{bbox}})["highway"="bus_stop"]["ref"~"{{ref_rx}}",i];
  node({{bbox}})["public_transport"="platform"]["ref"~"{{ref_rx}}",i];
  relation({{bbox}})["type"="route"]["route"="bus"]["ref"~"{{ref_rx}}",i];
);
out tags;
