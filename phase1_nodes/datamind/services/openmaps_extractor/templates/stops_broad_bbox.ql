[out:json][timeout:{{timeout_s}}];
(
  node({{bbox}})["highway"="bus_stop"];
  node({{bbox}})["public_transport"="platform"];
  node({{bbox}})["public_transport"="stop_position"];
  node({{bbox}})["railway"="tram_stop"];
  node({{bbox}})["amenity"="bus_station"];
);
out body;
