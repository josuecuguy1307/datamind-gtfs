SELECT ST_Equals(ST_StartPoint(sh.shape),s.stop_loc::geometry)
FROM shapes_aggregated sh JOIN stops s ON s.stop_id='S1' WHERE sh.shape_id='shape_0';
