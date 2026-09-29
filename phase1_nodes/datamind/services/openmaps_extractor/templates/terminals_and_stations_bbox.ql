[out:json][timeout:{{timeout_s}}];
(
  node({{bbox}})["amenity"="bus_station"];
  way({{bbox}})["amenity"="bus_station"];
  relation({{bbox}})["amenity"="bus_station"];

  node({{bbox}})["public_transport"="station"];
  way({{bbox}})["public_transport"="station"];

  node({{bbox}})["railway"="station"];
  node({{bbox}})["railway"="halt"];
);
out center;
