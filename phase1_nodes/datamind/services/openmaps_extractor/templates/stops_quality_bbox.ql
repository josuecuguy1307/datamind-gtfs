[out:json][timeout:{{timeout_s}}];
(
  node({{bbox}})["highway"="bus_stop"]["name"];
  node({{bbox}})["highway"="bus_stop"]["ref"];
  node({{bbox}})["highway"="bus_stop"]["operator"];

  node({{bbox}})["public_transport"="platform"]["name"];
  node({{bbox}})["public_transport"="platform"]["ref"];
);
out body;
