INSERT INTO "service" VALUES(1,'synthetic');
INSERT INTO "health_check" VALUES(1,'http',300,1,0,0,'idle','synthetic 1',0,'{}','https://example.invalid');
INSERT INTO "health_check" VALUES(2,'http',300,1,0,0,'idle','synthetic 2',0,'{}','https://example.invalid');
INSERT INTO "check_notification_state" VALUES(1,121,121,0,1800000000,1800000000,0,0,0);
INSERT INTO "check_notification_state" VALUES(2,1,0,0,0,1800000000,0,0,0);
