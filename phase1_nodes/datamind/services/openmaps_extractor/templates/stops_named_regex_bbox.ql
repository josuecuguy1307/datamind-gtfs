[out:json][timeout:{{timeout_s}}];
(
  node({{bbox}})["highway"="bus_stop"]["name"~"{{name_rx}}",i];
  node({{bbox}})["public_transport"="platform"]["name"~"{{name_rx}}",i];
  node({{bbox}})["public_transport"="stop_position"]["name"~"{{name_rx}}",i];
);
out body;
