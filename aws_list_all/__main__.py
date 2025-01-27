#!/usr/bin/env python
import json
import os
from argparse import ArgumentParser
from platform import python_version_tuple
from sys import exit, stderr

from botocore.loaders import Loader

from aws_list_all.client import get_client
from introspection import (
    get_listing_operations, get_services, get_verbs, introspect_regions_for_service, recreate_caches
)
from query import do_list_files, do_query, RESULT_NOTHING, RESULT_SOMETHING, RESULT_NO_ACCESS, RESULT_ERROR

# from gooey import Gooey

CAN_SET_OPEN_FILE_LIMIT = False
try:
    from resource import getrlimit, setrlimit, RLIMIT_NOFILE

    CAN_SET_OPEN_FILE_LIMIT = True
except ImportError:
    pass

python_major, python_minor, _ = python_version_tuple()
if int(python_major) < 3 or (int(python_major) == 3 and int(python_minor) < 7):
    print("WARNING: Unsupported python version. The program may crash now.")


def increase_limit_nofiles():
    if not CAN_SET_OPEN_FILE_LIMIT:
        print("Warning: Cannot import module 'resource' necessary to change open file limits.")
        print("This is expected if you run Windows without WSL.")
        return
    soft_limit, hard_limit = getrlimit(RLIMIT_NOFILE)
    desired_limit = 6000  # This should be comfortably larger than the product of services and regions
    if hard_limit < desired_limit:
        print("-" * 80, file=stderr)
        print(
            "WARNING!\n"
            "Your system limits the number of open files and network connections to {}.\n"
            "This may lead to failures during querying.\n"
            "Please increase the hard limit of open files to at least {}.\n"
            "The configuration for hard limits is often found in /etc/security/limits.conf".format(
                hard_limit, desired_limit
            ),
            file=stderr
        )
        print("-" * 80, file=stderr)
        print(file=stderr)
    target_soft_limit = min(desired_limit, hard_limit)
    if target_soft_limit > soft_limit:
        print("Increasing the open connection limit \"nofile\" from {} to {}.".format(soft_limit, target_soft_limit))
        setrlimit(RLIMIT_NOFILE, (target_soft_limit, hard_limit))
    print("")


# @Gooey
def restructure(data):
    new_data = {}
    for data_type in data.keys():
        new_data[data_type] = {}

    for data_type in data.keys():
        for item in data[data_type]:
            region = item[0]
            service = item[1]
            operation = item[2]
            result_types = item[3].split(", ")

            if region not in new_data[data_type]:
                new_data[data_type][region] = {}
            if service not in new_data[data_type][region]:
                new_data[data_type][region][service] = []

            new_data[data_type][region][service].append({
                "operation": operation,
                "result_types": result_types
            })
    return new_data


def main():
    """Parse CLI arguments to either list services, operations, queries or existing json files"""
    parser = ArgumentParser(
        prog='aws_list_all',
        description=(
            'List AWS resources on one account across regions and services. '
            'Saves result into json files, which can then be passed to this tool again '
            'to list the contents.'
        )
    )
    subparsers = parser.add_subparsers(
        description='List of subcommands. Use <subcommand> --help for more parameters',
        dest='command',
        metavar='COMMAND'
    )

    # Query is the main subcommand, so we put it first
    query = subparsers.add_parser('query', description='Query AWS for resources', help='Query AWS for resources')
    query.add_argument(
        '-s',
        '--service',
        action='append',
        help='Restrict querying to the given service (can be specified multiple times)'
    )
    query.add_argument(
        '-r',
        '--region',
        action='append',
        help='Restrict querying to the given region (can be specified multiple times)'
    )
    query.add_argument(
        '-o',
        '--operation',
        action='append',
        help='Restrict querying to the given operation (can be specified multiple times)'
    )
    query.add_argument('-p', '--parallel', default=32, type=int, help='Number of request to do in parallel')
    query.add_argument('-d', '--directory', default='.', help='Directory to save result listings to')
    query.add_argument('-v', '--verbose', action='count', help='Print detailed info during run')
    query.add_argument('-c', '--profile', help='Use a specific .aws/credentials profile.')

    # Once you have queried, show is the next most important command. So it comes second
    show = subparsers.add_parser(
        'show', description='Show a summary or details of a saved listing', help='Display saved listings'
    )
    show.add_argument('listingfile', nargs='*', help='listing file(s) to load and print')
    show.add_argument('-v', '--verbose', action='count', help='print given listing files with detailed info')

    # Introspection debugging is not the main function. So we put it all into a subcommand.
    introspect = subparsers.add_parser(
        'introspect',
        description='Print introspection debugging information',
        help='Print introspection debugging information'
    )
    introspecters = introspect.add_subparsers(
        description='Pieces of debug information to collect. Use <DETAIL> --help for more parameters',
        dest='introspect',
        metavar='DETAIL'
    )

    introspecters.add_parser(
        'list-services',
        description='Lists short names of AWS services that the current boto3 version has clients for.',
        help='List available AWS services'
    )
    introspecters.add_parser(
        'list-service-regions',
        description='Lists regions where AWS services are said to be available.',
        help='List AWS service regions'
    )
    ops = introspecters.add_parser(
        'list-operations',
        description='List all discovered listing operations on all (or specified) services',
        help='List discovered listing operations'
    )
    ops.add_argument(
        '-s',
        '--service',
        action='append',
        help='Only list discovered operations of the given service (can be specified multiple times)'
    )
    introspecters.add_parser('debug', description='Debug information', help='Debug information')

    # Finally, refreshing the service/region caches comes last.
    caches = subparsers.add_parser(
        'recreate-caches',
        description=(
            'The list of AWS services and endpoints can change over time. '
            'This command (re-)creates the caches for this data to allow you to'
            'list services in regions where they have not been available previously.'
            'The cache lives in your OS-dependent cache directory, e.g. ~/.cache/aws_list_all/'
        ),
        help='Recreate service and region caches'
    )
    caches.add_argument(
        '--update-packaged-values',
        action='store_true',
        help=(
            'Instead of writing to the cache, update files packaged with aws-list-all. '
            'Use this only if you run a copy from git.'
        )
    )

    args = parser.parse_args()

    if args.command == 'query':
        if args.directory:
            try:
                os.makedirs(args.directory)
            except OSError as e:
                pass
            os.chdir(args.directory)
        increase_limit_nofiles()
        services = args.service or get_services()
        results_by_type = do_query(
            services,
            args.region,
            args.operation,
            verbose=args.verbose or 0,
            parallel=args.parallel,
            selected_profile=args.profile
        )
        results_by_type = restructure(results_by_type)
        with open('../aws_list_all.json', 'w') as f:
            json.dump(results_by_type, f, indent=2)
        print("Wrote results to aws_list_all.json")
        for result_type in (RESULT_SOMETHING, RESULT_NO_ACCESS, RESULT_ERROR):
            result = sorted(results_by_type[result_type])
            for result in result:
                print(*result)
    elif args.command == 'show':
        if args.listingfile:
            increase_limit_nofiles()
            do_list_files(args.listingfile, verbose=args.verbose or 0)
        else:
            show.print_help()
            return 1
    elif args.command == 'introspect':
        if args.introspect == 'list-services':
            for service in get_services():
                print(service)
        elif args.introspect == 'list-service-regions':
            introspect_regions_for_service()
            return 0
        elif args.introspect == 'list-operations':
            for service in args.service or get_services():
                for operation in get_listing_operations(service):
                    print(service, operation)
        elif args.introspect == 'debug':
            for service in get_services():
                for verb in get_verbs(service):
                    print(service, verb)
        else:
            introspect.print_help()
            return 1
    elif args.command == 'recreate-caches':
        increase_limit_nofiles()
        recreate_caches(args.update_packaged_values)
    else:
        parser.print_help()
        return 1


if __name__ == '__main__':
    client, session = get_client('ecs')
    s = Loader().list_available_services('region-1')
    exit(main())
