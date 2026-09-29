[out:json][timeout:{{timeout_s}}];
(
  node({{bbox}})["highway"="bus_stop"]["operator"~"{{operator_rx}}",i];
  node({{bbox}})["public_transport"="platform"]["operator"~"{{operator_rx}}",i];
  node({{bbox}})["amenity"="bus_station"]["operator"~"{{operator_rx}}",i];
);
out body;
