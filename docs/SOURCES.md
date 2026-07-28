# Authoritative sources and references

Per-network documentation, APIs, portals, and design literature
(DESIGN.md §7).  Per-client GeoJSONs carry the same links in their
`metadata.references` blocks.

## Cross-network

- USBR *Emerging Technologies in Snow Monitoring* — technical appendix
  surveying in-situ snow measurement techniques and networks:
  <https://www.usbr.gov/research/docs/news/Emerging_Snow_Monitoring_Report_Technical_Apprendix_508.pdf>
- NorSWE / CanSWE curated SWE datasets (aggregations that overlap this
  archive's stations — tracked in issue #22):
  <https://zenodo.org/records/15263370>, <https://zenodo.org/records/19075529>
- This repository's design contract: [DESIGN.md](../DESIGN.md)

## AWDB (SNOTEL, SNOLite, MSNT, SCAN, COOP, SNOW, MPRC)

- AWDB REST API v1 (Swagger):
  <https://wcc.sc.egov.usda.gov/awdbRestApi/swagger-ui/index.html>
- NRCS demo notebooks: <https://github.com/nrcs-nwcc/iow_awdb_rest_api_demo>
- SNOTEL program: <https://www.nrcs.usda.gov/programs-initiatives/snotel-snow-telemetry>
- Interactive map: <https://nwcc-apps.sc.egov.usda.gov/imap/>
- Report generator: <https://wcc.sc.egov.usda.gov/reportGenerator/>
- Air-temperature bias correction status (fetched live at build time):
  <https://www.wcc.nrcs.usda.gov/ftpref/support/air_temp_bias/nrcs_air_temp_unbias.html>

## CDEC (California Cooperative Snow Surveys)

- CDEC portal: <https://cdec.water.ca.gov>
- Station metadata pages: `https://cdec.water.ca.gov/dynamicapp/staMeta?station_id={ID}`
- JSON data service: `https://cdec.water.ca.gov/dynamicapp/req/JSONDataServlet`
- CCSS program: <https://water.ca.gov/Programs/Flood-Management/Snow-Surveys>

## DataBC (BC Snow Survey — ASWS + MSS)

- BC Data Catalogue: <https://catalogue.data.gov.bc.ca>
- ASWS/MSS CSV directory: <https://www.env.gov.bc.ca/wsd/data_searches/snow/asws/data/>
- AQRT station portal: `https://aqrt.nrs.gov.bc.ca/Data/Location/Summary/Location/{ID}/Interval/Latest`
- Snow-station satellite cameras (the `station_camera_url` source):
  <https://www2.gov.bc.ca/gov/content/environment/air-land-water/water/water-science-data/water-data-tools/snow-survey-data/snow-station-satellite-cameras>
- BC River Forecast Centre: <https://www2.gov.bc.ca/gov/content/environment/air-land-water/water/drought-flooding-dikes-dams/river-forecast-centre>

## NVE (Norway)

- HydAPI documentation: <https://hydapi.nve.no/UserDocumentation/>
- Sildre station portal: <https://sildre.nve.no/>
- xgeo.no national snow map: <https://www.xgeo.no/>
- seNorge gridded products (context, not point obs): <http://www.senorge.no/>

## Yukon (AquaCache)

- Water Data Explorer: <https://service.yukon.ca/water-data/shiny/>
- OpenAPI spec: <https://service.yukon.ca/water-data/api/v1/openapi.json>
- Open Yukon — Snow Survey Network:
  <https://open.yukon.ca/data/yukon-snow-survey-network>
- Snow surveys and water supply forecasts:
  <https://yukon.ca/en/science-and-natural-resources/water/snow-surveys-and-water-supply-forecasts>
- AquaCache upstream project: <https://github.com/YukonWRB/AquaCache>
