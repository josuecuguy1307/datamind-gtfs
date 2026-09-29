[out:json][timeout:{{timeout_s}}];

(
  node({{bbox}})["public_transport"="platform"];
);
out body;

(
  way({{bbox}})["public_transport"="platform"];
  relation({{bbox}})["public_transport"="platform"];
);
out center;
