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

## Hydro-Québec

- Open data dataset (Opendatasoft):
  <https://donnees.hydroquebec.com/explore/dataset/donnees-hydrometeorologiques/>
- API: <https://donnees.hydroquebec.com/api/explore/v2.1/catalog/datasets/donnees-hydrometeorologiques>
- Upstream JSON the portal flattens:
  <https://www.hydroquebec.com/data/documents-donnees/donnees-ouvertes/json/Donnees_VUE_STATIONS_ET_TARAGES.json>
- Licence, CC BY-NC 4.0: <https://creativecommons.org/licenses/by-nc/4.0/>
- GMON history (CanSWE, Vionnet et al.): <https://doi.org/10.5281/zenodo.4734371>

## Alberta River Basins

- Alberta River Basins web app: <https://rivers.alberta.ca/>
- Station catalog (ArcGIS MapServer, reached through the app's proxy):
  <https://geospatial.alberta.ca/europa/rest/services/arb/alberta_river_basins/MapServer>
- Government of Alberta copyright and disclaimer:
  <https://www.alberta.ca/disclaimer.aspx>

## SLF (Switzerland)

- SLF Data Service terms: <https://www.slf.ch/en/services-and-products/slf-data-service/>
- IMIS automatic stations: <https://measurement-data.slf.ch/imis/>,
  DOI <https://doi.org/10.16904/envidat.406>
- Manual measuring network (study plots):
  <https://measurement-data.slf.ch/study-plot/>, DOI
  <https://doi.org/10.16904/envidat.408>
- GCOS SWE data (11 sites, biweekly): <https://www.envidat.ch/dataset/gcos-swe-data>,
  DOI <https://doi.org/10.16904/15>
