# coding: utf-8

import time
import json
import datetime
from pyspark.sql import SparkSession, Row, SQLContext, Window
from pyspark.sql.types import StructType, StringType, StructField, BooleanType, IntegerType, ArrayType, TimestampType, \
    DoubleType
from pyspark import SparkContext
import pyspark.sql.functions as f
from common.job import Job
from common.encounter_helper import EncounterHelper
from pyspark.streaming.kafka import KafkaUtils


class EncounterJob(Job):

    # responsible for ingesting obs with non-null encounters
    def ingest_obs_with_encounter(self,encounter_ids):
        encounter_ids=','.join(map(str, encounter_ids))
        query = """(SELECT * FROM obs_with_encounter
               where encounter_id in ({0}) and (voided is null or voided = 0)) AS tmp
              """.format(encounter_ids)

        obs = super().getDataFromMySQL(query,None)
        return EncounterHelper.sanitize_obs(obs)

    # responsible for ingesting obs with null encounters
    def ingest_obs_without_encounter(self,patient_ids):
        patient_ids=','.join(map(str, patient_ids))
        query ="""(SELECT * FROM obs_with_null_encounter 
                        where patient_id in ({0}) and (voided is null or voided = 0)) AS tmp
                        """.format(patient_ids)
    
        obs = super().getDataFromMySQL(query,None)\
            .withColumn('encounter_id', f.col('obs_id') + 100000000) \
            .withColumn('order_id', f.lit('null')) \
            .withColumn('order_concept_id', f.lit('null'))
        return EncounterHelper.sanitize_obs(obs)

    # responsible for ingesting orders
    def ingest_orders(self,encounter_ids):
        encounter_ids=','.join(map(str, encounter_ids))
        query ="""(SELECT * FROM encounter_orders
                        where encounter_id in ({0}) 
                        and (voided is null or voided = 0)) AS tmp""".format(encounter_ids)
        orders = super().getDataFromMySQL(query,None)                
        return EncounterHelper.sanitize_orders(orders)

    # responsible for rebuilding changed data
    def run_microbatch(self, rdd):
        try:

            collected = rdd.collect()

            if len(collected) > 0:
                start_time = datetime.datetime.utcnow()
                encounter_ids = []
                person_ids = []
                
                print("---Micro-Batch--- ")
                print("Starting calculations for flat_enc_obs_orders " + time.ctime())
                for person in collected:
                    person_ids.append(person["person_id"])
                    for encounter in person["encounters"]:
                        encounter_ids.append(encounter)

                print("Obs CDC: Patient IDs ", person_ids)
                print("Order CDC: Enc IDs ", encounter_ids)

                # ingest all components
                obs_with_encounter = self.ingest_obs_with_encounter(person_ids) 
                obs_without_encounter = self.ingest_obs_without_encounter(person_ids)
                orders = self.ingest_orders(encounter_ids)
                
                # union obs and join
                all_obs = obs_with_encounter.union(obs_without_encounter)
                obs_orders = EncounterHelper.join_obs_orders(all_obs,orders)

                #TODO save to Data Lake
                
                end_time = datetime.datetime.utcnow()
                print("Took {0} seconds".format((end_time - start_time).total_seconds()))


        except:
            print("An unexpected error occurred")
            raise

    # start spark streaming job
    def run(self):

        print("Encounter Streaming Job Started at =", datetime.datetime.utcnow())
        topic='encounter-obs-orders' # define topic/s
        kafka_config = super().getConfig()['kafka'][topic]
        ssc= super().getStreamingContext()
        kafka_stream = KafkaUtils\
              .createDirectStream(ssc,topics=kafka_config['topics'],kafkaParams=kafka_config['config']) \
              .map(lambda msg: json.loads(msg[1]))

        obs_stream = kafka_stream \
                .filter(lambda msg: 'obs.Envelope' in msg['schema']['name']) \
                .map(lambda msg: msg['payload']['after']) \
                .map(lambda a: Row(**a))

        orders_stream = kafka_stream \
                .filter(lambda msg: 'orders.Envelope' in msg['schema']['name']) \
                .map(lambda msg: msg['payload']['after'])\
                .map(lambda a: Row(**a))
        
        # separate obs with null encounters from obs with encounters
        obs_with_enc_stream = obs_stream.filter(lambda a: a['encounter_id'] is not None)
        obs_with_null_enc_stream  = obs_stream.filter(lambda a: a['encounter_id'] is None)
        
        #convert orders and obs with encounters into a tuple of encouter_id and person_id
        orders_stream = orders_stream.map(lambda row: (row['encounter_id'], row['patient_id']))
        obs_with_enc_stream = obs_with_enc_stream.map(lambda row: (row['encounter_id'], row['person_id']))

        #union the orders and obs stream
        enc_obs_orders = obs_with_enc_stream.union(orders_stream)

        #extract distinct encounter id
        enc_obs_orders = enc_obs_orders.reduceByKey(lambda x, y: x)
        
        #convert into (person_id, encounter_id) tuple from (encounter_id, person_id) tuple
        enc_obs_orders = enc_obs_orders.map(lambda tpl: (tpl[1], tpl[0]))
        
        #convert obs without encounters into person_id, None tuple
        obs_with_null_enc = obs_with_null_enc_stream.map(lambda row: (row['person_id'], None))
        
        #join obs without encounters with the enc_obs_orders for processing
        enc_obs_orders = enc_obs_orders.union(obs_with_null_enc)
        
        #group by patient_id to get distinct patients
        enc_obs_orders = enc_obs_orders.groupByKey()\
            .map(lambda x : Row(person_id=x[0], encounters=list(filter(None.__ne__, x[1]))))
        
        enc_obs_orders.foreachRDD(lambda rdd: self.run_microbatch(rdd))

        ssc.start()
        ssc.awaitTermination()