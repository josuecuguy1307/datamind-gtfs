[out:json][timeout:{{timeout_s}}];
(
  node({{bbox}})["amenity"~"{{amenity_rx}}",i];
  way({{bbox}})["amenity"~"{{amenity_rx}}",i];
  node({{bbox}})["shop"~"{{shop_rx}}",i];
  node({{bbox}})["tourism"~"{{tourism_rx}}",i];
);
out center;
