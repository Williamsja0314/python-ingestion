#!/usr/bin/env python
'''
Usage: python ingest.py from_csv <ingestion-parameters>.csv
This script returns error codes at various points-of-failure:
    4 - There is a problem with the EDEX server.
    5 - An integer value was not specified for the --sleep option.
'''

import csv
import datetime
import logging
import subprocess
import sys
import time
from config import SLEEP_TIMER, UFRAME, EDEX
from whelk import shell
from glob import glob


EDEX_LOG_FILES = glob("%s%s" % (EDEX['log_path'], "edex-ooi*.log"))
EDEX_LOG_FILES += glob("%s%s" % (EDEX['log_path'], "*.zip"))

# Set up some basic logging.
logging.basicConfig(level=logging.INFO)
handler = logging.FileHandler(
    UFRAME['log_path'] + '/' + 'ingestion.log'
    )
handler.setLevel(logging.INFO)
handler.setFormatter(
    logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    )
logger = logging.getLogger(__name__)
logger.addHandler(handler)

class Task(object):
    ''' A helper class designed to manage the different types of ingestion tasks.'''

    # A list of methods on the Task class that are valid ingestion tasks.
    valid_tasks = (
        'dummy',            # A dummy task. Doesn't do anything.
        'from_csv',         # Ingest from a single CSV file.
        'from_csv_batch',   # Ingest from multiple CSV files defined in a batch.
        ) 

    def __init__(self, args):
        ''' Parse and interpret common command-line options.'''
        test_mode = "-t" in args
        force_mode = "-f" in args
        sleep_timer = SLEEP_TIMER
        try:
            sleep_timer = int([a for a in args if a[:8]=="--sleep="][0].split("=")[1])
        except IndexError:
            pass
        except ValueError:
            logger.error("--sleep must be set to an integer")
            sys.exit(5)
        self.options = {
            'test_mode': test_mode, 
            'force_mode': force_mode,
            'sleep_timer': sleep_timer,
            }
        self.args = args

    def dummy(self):
        ''' A dummy task that doesn't do anything except create an Ingestor.'''
        ingest = Ingestor(self.options)
        logger.info("Dummy task was run.")

    def from_csv(self):
        ''' Ingest data mapped out by a single CSV file. '''

        # Check to see if a valid CSV has been specified.
        try:
            csv_file = [f for f in self.args if f[-4:].lower()==".csv"][0]
        except IndexError:
            logger.error("No mapping CSV specified.")
            return False

        # Create an instance of the Ingestor class with common options set.
        ingest = Ingestor(self.options)

        # Start the EDEX services and run the ingestion from the CSV file.
        ingest.from_csv(csv_file)

        # Write out any failed ingestions to a new CSV file.
        if ingest.failed_ingestions:
            ingest.write_failures_to_csv(
                csv_file.split("/")[-1].split(".")[0])
        return True

    def from_csv_batch(self):
        ''' Ingest data mapped out by multiple CSV files, defined in a single .csv.batch file.'''

        # Create an instance of the Ingestor class with common options set.
        ingest = Ingestor(self.options)

        # Open the batch list file and parse the csv paths into a list
        try:
            csv_batch = [f for f in self.args if f[-10:].lower()==".csv.batch"][0]
        except IndexError:
            logger.error("No CSV batch file specified.")
            return False
        with open(csv_batch, 'r') as f:
            csv_files = [x.strip() for x in f.readlines() if x.strip()]

        # Start the EDEX services and ingest from each CSV file.
        for csv_file in csv_files:
            ingest.from_csv(csv_file)

        # Write out any failed ingestions from the entire batch to a new CSV file.
        if ingest.failed_ingestions:
            ingest.write_failures_to_csv(
                csv_batch.split("/")[-1].split(".")[0] + "_batch")
        return True

class ServiceManager(object):
    def __init__(self, options):
        self.test_mode = options.get('test_mode', False)
        self.edex_command = options.get('edex_command', EDEX['command'])

        # Source the EDEX server environment.
        if self.test_mode or EDEX['test_mode']:
            logger.info("TEST MODE: Sourcing the EDEX server environment.")
            logger.info("TEST MODE: EDEX server environment sourced.")
            return

        # Source the EDEX environment.
        try:
            logger.info("Sourcing the EDEX server environment.")
            subprocess.check_output(['source', self.edex_command])
        except Exception:
            logger.exception(
                "An error occurred when sourcing the EDEX server environment.")
            sys.exit(4)
        else:
            logger.info("EDEX server environment sourced.")

    def start(self):
        logger.info("Starting all services.")
        start_command = [self.edex_command, "all", "start"]
        try:
            if self.test_mode:
                logger.info("TEST MODE: %s" % start_command)
            else:
                subprocess.check_output(start_command)
        except Exception:
            logger.exception(
                "An error occurred when starting the system services.")
            sys.exit(4)
        else:
            logger.info("Checking service statuses and refreshing process IDs.")
            self.refresh_status()
            logger.info("Services started")

    def restart(self, stale_process_ids):
        self.kill(stale_process_ids)
        self.start()

    def kill(stale_process_ids):
        for pid in stale_process_ids.itervalues():
            pass

    def refresh_status(self):
        self.process_ids = {}
        try:
            if self.test_mode:
                status = "edex_ooi:   632\npostgres:   732\nqpidd:   845\npypies: 948 7803 7943 7944 7945 8037 8142 8143 8144\n"
            else:
                status = subprocess.check_output([self.edex_command, "all", "status"])
        except Exception:
            logger.exception(
                "An error occurred when checking the service statuses.")
            sys.exit(4)
        else:
            status = [s.strip() for s in status.split('\n') if s.strip()]
            for s in status:
                name, value = s.split(":")
                value = value.strip().split(" ")
                if len(value) == 1:
                    value = value[0]
                self.process_ids[name] = value
            self.process_ids['edex_wrapper'] = shell.pgrep("-P", self.process_ids["edex_ooi"])[1]
            if self.test_mode:
                self.process_ids['edex_wrapper'], self.process_ids['edex_server'] = "test", "test"
            else:
                if self.process_ids['edex_wrapper']:
                    self.process_ids['edex_server'] = shell.pgrep("-P", self.process_ids["edex_wrapper"])[1]
                else:
                    self.process_ids['edex_server'] = None
        return all(self.process_ids.itervalues())

class Ingestor(object):
    ''' A helper class designed to handle the ingestion process.'''

    def __init__(self, options, service_manager=None):
        self.service_manager = service_manager or ServiceManager(options)
        self.service_manager.start()

        self.test_mode = options.get('test_mode', False)
        self.force_mode = options.get('force_mode', False)
        self.sleep_timer = options.get('sleep_timer', SLEEP_TIMER)
        self.failed_ingestions = []

    def send(self, filename_mask, uframe_route, reference_designator, data_source):
        ''' A helper method that finds the files that match the provided filename mask and calls 
            UFrame's ingest sender application with the appropriate command-line arguments. '''

        # Define some helper methods.
        def annotate_parameters(file, route, designator, source):
            ''' Turn the ingestion parameters into a dictionary with descriptive keys.'''
            return {
                'filename_mask': file, 
                'uframe_route': route, 
                'reference_designator': designator, 
                'data_source': source,
                }
        def in_edex_log(datafile):
            ''' Check EDEX logs to see if the file has been ingested by EDEX.'''
            return bool(shell.zgrep(datafile, *EDEX_LOG_FILES))

        # Get a list of files that match the file mask and log the list size.
        data_files = glob(filename_mask)
        logger.info("%s file(s) found for %s" % (len(data_files), filename_mask))

        # If no files are found, consider the entire filename mask a failure and track it.
        if len(data_files) == 0:
            self.failed_ingestions.append(
                annotate_parameters(filename_mask, uframe_route, reference_designator, data_source))
            return False

        # Ingest each file in the file list.
        previous_data_file = ""
        for data_file in data_files:
            # Check if the EDEX services are still running. If not, attempt to restart them.
            while True:
                stale_process_ids = self.service_manager.process_ids
                if self.service_manager.refresh_status():
                    break
                logger.warn((
                    "One or more EDEX services crashed after ingesting the previous data file "
                    "(%s). Attempting to restart services." % previous_data_file
                    ))
                self.service_manager.restart(stale_process_ids)

            # Check if the data_file has previously been ingested. If it has, then skip it, unless 
            # force mode (-f) is active.
            if in_edex_log(data_file):
                if self.force_mode:
                    logger.warning((
                        "EDEX logs indicate that %s has already been ingested, "
                        "but force mode (-f) is active. The file will be reingested.") % data_file)
                else:
                    logger.warning((
                        "EDEX logs indicate that %s has already been ingested. "
                        "The file will not be reingested.") % data_file)
                    continue

            ingestion_command = (
                UFRAME['command'], uframe_route, data_file, reference_designator, data_source)
            try:
                # Attempt to send the file to UFrame's ingest sender.
                ingestion_command_string = " ".join(ingestion_command)
                if self.test_mode:
                    ingestion_command_string = "TEST MODE: " + ingestion_command_string
                else:
                    subprocess.check_output(ingestion_command)
            except subprocess.CalledProcessError as e:
                # If UFrame's ingest sender fails and returns a non-zero exit code, log it.
                logger.error(
                    "There was a problem with UFrame when ingesting %s (Error code %s)." % (
                        data_file, e.returncode))
                self.failed_ingestions.append(
                    annotate_parameters(data_file, uframe_route, reference_designator, data_source))
            except Exception:
                # If there is some other system issue, log it with traceback.
                logger.exception(
                    "There was an unexpected system error when ingesting %s" % data_file)
                self.failed_ingestions.append(
                    annotate_parameters(data_file, uframe_route, reference_designator, data_source))
            else:
                # If there are no errors, consider the ingest send a success and log it.
                logger.info(ingestion_command_string)
            previous_data_file = data_file
        time.sleep(self.sleep_timer)
        return True

    def from_csv(self, csv_file):
        ''' Reads the specified CSV file for mask, route, designator, and source parameters and 
            calls the ingest sender method with the appropriate parameters. '''

        try:
            reader = csv.DictReader(open(csv_file))
        except IOError:
            logger.error("%s not found." % csv_file)
            return False
        fieldnames = ['uframe_route', 'filename_mask', 'reference_designator', 'data_source']
        if reader.fieldnames != fieldnames:
            logger.error("%s does not have valid column headers." % csv_file)
            return False

        # Run ingestions for each row in the CSV, keeping track of any failures.
        for row in reader:
            self.send(
                row['filename_mask'],
                row['uframe_route'], 
                row['reference_designator'], 
                row['data_source'],
                )
        logger.info(
            "Ingestion task from_csv for %s completed with %s failure(s)." % (
                csv_file, len(self.failed_ingestions)))

    def write_failures_to_csv(self, label):
        ''' Write any failed ingestions out into a CSV file that can be re-ingested later. '''

        date_string = datetime.datetime.today().strftime('%Y_%m_%d')
        fieldnames = ['uframe_route', 'filename_mask', 'reference_designator', 'data_source']
        outfile = "%s/failed_ingestions_%s_%s.csv" % (
            UFRAME["failed_ingestion_path"], label, date_string)

        writer = csv.DictWriter(
            open(outfile, 'wb'), delimiter=',', fieldnames=fieldnames)

        logger.info(
            "Writing %s failed ingestion(s) out to %s" % (len(self.failed_ingestions), outfile))
        writer.writerow(dict((fn,fn) for fn in fieldnames))
        for f in self.failed_ingestions:
            writer.writerow(f)

if __name__ == '__main__':
    task, args = sys.argv[1], sys.argv[2:]
    perform = Task(args)
    if task in Task.valid_tasks:
        logger.info(
            "Running ingestion task '%s' with command-line arguments '%s'" % (
                task, " ".join(args)))
        try:
            getattr(perform, task)()
        except Exception:
            logger.exception("There was an unexpected error.")
    else:
        logger.error("%s is not a valid ingestion task." % task)
    