import itertools
import operator

from janitor.actions import ActionRegistry, BaseAction
from janitor.filters import (
    FilterRegistry, AgeFilter, ValueFilter
)

from janitor.manager import ResourceManager, resources
from janitor.offhours import Time, OffHour, OnHour
from janitor import tags, utils


filters = FilterRegistry('ec2.filters')
actions = ActionRegistry('ec2.actions')

filters.register('time', Time)
tags.register_tags(filters, actions, 'InstanceId')


@resources.register('ec2')
class EC2(ResourceManager):

    def __init__(self, ctx, data):
        super(EC2, self).__init__(ctx, data)
        # FIXME: should we be doing this check in every ResourceManager?
        if not isinstance(self.data, dict):
            raise ValueError(
                "Invalid format, expecting dictionary found %s" % (
                    type(self.data)))
        self.queries = QueryFilter.parse(self.data.get('query', []))
        self.filters = filters.parse(self.data.get('filters', []), self)
        self.actions = actions.parse(self.data.get('actions', []), self)

    @property
    def client(self):
        return self.session_factory().client('ec2')

    def get_resources(self, resource_ids):
        return utils.query_instances(
            None,
            client=self.session_factory().client('ec2'),
            InstanceIds=resource_ids)

    def resources(self):
        qf = self.resource_query()
        instances = None

        if self._cache.load():
            instances = self._cache.get(qf)
        if instances is not None:
            self.log.info(
                'Using cached instance query: %s instances' % len(instances))
            return self.filter_resources(instances)

        self.log.info("Querying ec2 instances with %s" % qf)
        session = self.session_factory()
        client = session.client('ec2')
        p = client.get_paginator('describe_instances')

        results = p.paginate(Filters=qf)
        reservations = list(
            itertools.chain(*[pp['Reservations'] for pp in results]))
        instances =  list(itertools.chain(
            *[r["Instances"] for r in reservations]))
        self.log.debug("Found %d instances on %d reservations" % (
            len(instances), len(reservations)))
        self._cache.save(qf, instances)

        # Filter instances
        return self.filter_resources(instances)
    
    def format_json(self, resources, fh):
        resources = sorted(
            resources, key=operator.itemgetter('LaunchTime'))
        utils.dumps(resources, fh, indent=2)

    def resource_query(self):
        qf = []
        qf_names = set()
        # allow same name to be specified multiple times and append the queries
        # under the same name
        for q in self.queries:
            qd = q.query()
            if qd['Name'] in qf_names:
                for qf in qf:
                    if qd['Name'] == qf['Name']:
                        qf['Values'].extend(qd['Values'])
            else:
                qf_names.add(qd['Name'])
                qf.append(qd)
        return qf


class StateTransitionFilter(object):
    """Filter instances by state.

    Try to simplify construction for policy authors by automatically
    filtering elements (filters or actions) to the instances states
    they are valid for.
    
    For more details see http://goo.gl/TZH9Q5

    """
    valid_origin_states = ()

    def filter_instance_state(self, instances):
        orig_length = len(instances)
        results = [i for i in instances
                   if i['State']['Name'] in self.valid_origin_states]
        self.log.info("%s %d of %d instances" % (
            self.__class__.__name__, len(results), orig_length))
        return results


@filters.register('ebs')
class AttachedVolume(ValueFilter):

    def process(self, resources, event=None):
        self.volume_map = self.get_volume_mapping(resources)
        self.operator = self.data.get(
            'operator', 'and') == 'and' and all or any
        return filter(self, resources)

    def get_volume_mapping(self, resources):
        volume_map = {}
        ec2 = utils.local_session(self.manager.session_factory).client('ec2')
        for instance_set in utils.chunks(
                [i['InstanceId'] for i in resources], 200):
            self.log.debug("Processsing %d instance of %d" % (
                len(instance_set), len(resources)))
            results = ec2.describe_volumes(
                Filters=[
                    {'Name': 'attachment.instance-id',
                     'Values': instance_set}])
            for v in results['Volumes']:
                volume_map.setdefault(
                    v['Attachments'][0]['InstanceId'], []).append(v)
        return volume_map

    def __call__(self, i):
        volumes = self.volume_map.get(i['InstanceId'])
        if not volumes:
            return False
        return self.operator(map(self.match, volumes))

    
@filters.register('image')
class InstanceImage(ValueFilter):

    def process(self, resources, event=None):
        self.image_map = self.get_image_mapping(resources)

    def get_image_mapping(self, resources):
        ec2 = utils.local_session(self.manager.session_factory).client('ec2')
        image_ids = set([i['ImageId'] for i in resources])
        results = ec2.describe_images(ImageIds=list(image_ids))
        return {i['ImageId']: i for i in results['Images']}

    def __call__(self, i):
        image = self.image_map.get(i['InstanceId'])
        if not image:
            self.log.warning(
                "Could not locate image for instance:%s ami:%s" % (
                    i['InstanceId'], i["ImageId"]))
            # Match instead on empty skeleton?
            return False
        return self.match(image)
        
            
@filters.register('offhour')
class InstanceOffHour(OffHour, StateTransitionFilter):

    valid_origin_states = ('running',)

    def process(self, resources, event=None):
        return super(InstanceOffHour, self).process(
            self.filter_instance_state(resources))

    
@filters.register('onhour')
class InstanceOnHour(OnHour, StateTransitionFilter):
    
    valid_origin_states = ('stopped',)

    def process(self, resources, event=None):
        return super(InstanceOnHour, self).process(
            self.filter_instance_state(resources))


@filters.register('instance-uptime')
class UpTimeFilter(AgeFilter):

    date_attribute = "LaunchTime"

    
@filters.register('instance-age')        
class InstanceAgeFilter(AgeFilter):

    date_attribute = "LaunchTime"
    ebs_key_func = operator.itemgetter('AttachTime')

    def get_resource_date(self, i):
        # LaunchTime is basically how long has the instance
        # been on, use the oldest ebs vol attach time
        found = False
        ebs_vols = [
            block['Ebs'] for block in i['BlockDeviceMappings']
            if 'Ebs' in block]
        if not ebs_vols:
            # Fall back to using age attribute (ephemeral instances)
            return super(InstanceAgeFilter, self).get_resource_date(i)
        # Lexographical sort on date
        ebs_vols = sorted(ebs_vols, key=self.ebs_key_func)
        return ebs_vols[0]['AttachTime']
        
    
@actions.register('start')        
class Start(BaseAction, StateTransitionFilter):

    valid_origin_states = ('stopped',)

    def process(self, instances):
        instances = self.filter_instance_state(instances)
        if not len(instances):
            return
        self._run_api(
            self.manager.client.start_instances,
            InstanceIds=[i['InstanceId'] for i in instances],
            DryRun=self.manager.config.dryrun)


@actions.register('stop')
class Stop(BaseAction, StateTransitionFilter):
    """Stop instances
    """
    valid_origin_states = ('running', 'pending')

    def split_on_storage(self, instances):
        ephemeral = []
        persistent = []
        for i in instances:
            for bd in i.get('BlockDeviceMappings', []):
                if bd['DeviceName'] == '/dev/sda1':
                    if 'Ebs' in bd:
                        persistent.append(i)
                    else:
                        ephemeral.append(i)
        return ephemeral, persistent
    
    def process(self, instances):
        instances = self.filter_instance_state(instances)
        if not len(instances):
            return
        ephemeral, persistent = self.split_on_storage(instances)
        if self.data.get('terminate-ephemeral', False):
            self._run_api(
                self.manager.client.terminate_instances,
                InstanceIds=[i['InstanceId'] for i in ephemeral],
                DryRun=self.manager.config.dryrun)
        self._run_api(
            self.manager.client.stop_instances,
            InstanceIds=[i['InstanceId'] for i in persistent],
            DryRun=self.manager.config.dryrun)
        
        
@actions.register('terminate')        
class Terminate(BaseAction, StateTransitionFilter):
    """ Terminate a set of instances.
    
    While ec2 offers a bulk delete api, any given instance can be configured
    with api deletion termination protection, so we can't use the bulk call
    reliabily, we need to process the instances individually. Additionally
    If we're configured with 'force' then we'll turn off instance termination
    protection.
    """

    valid_origin_states = ('running', 'stopped', 'pending', 'stopping')
    
    def process(self, instances):
        instances = self.filter_instance_state(instances)
        if not len(instances):
            return
        if self.data.get('force'):
            self.log.info("Disabling termination protection on instances")
            self.disable_deletion_protection(instances)
        # limit batch sizes to avoid api limits
        for batch in utils.chunks(instances, 100):
            self._run_api(
                self.manager.client.terminate_instances,
                InstanceIds=[i['InstanceId'] for i in instances],
                DryRun=self.manager.config.dryrun)

    def disable_deletion_protection(self, instances):
        def process_instance(i):
            client = utils.local_session(
                self.manager.session_factory).client('ec2')
            self._run_api(
                client.modify_instance_attribute,
                InstanceId=i['InstanceId'],
                Attribute='disableApiTermination',
                Value='false',
                DryRun=self.manager.config.dryrun)

        with self.executor_factory(max_workers=2) as w:
            list(w.map(process_instance, instances))
            

# Valid EC2 Query Filters
# http://docs.aws.amazon.com/AWSEC2/latest/CommandLineReference/ApiReference-cmd-DescribeInstances.html
EC2_VALID_FILTERS = {
    'architecture': ('i386', 'x86_64'),
    'availability-zone': str,
    'iam-instance-profile.arn': str, 
    'image-id': str,
    'instance-id': str,
    'instance-lifecycle': ('spot',),
    'instance-state-name': (
        'pending',
        'terminated',
        'running',
        'shutting-down',
        'stopping',
        'stopped'),
    'instance.group-id': str,
    'instance.group-name': str,
    'tag-key': str,
    'tag-value': str,
    'tag:': str,
    'vpc-id': str}


class QueryFilter(object):

    @classmethod
    def parse(cls, data):
        results = []
        for d in data:
            if not isinstance(d, dict):
                raise ValueError(
                    "EC2 Query Filter Invalid structure %s" % d)
            results.append(cls(d).validate())
        return results

    def __init__(self, data):
        self.data = data
        self.key = None
        self.value = None
        
    def validate(self):
        if not len(self.data.keys()) == 1:
            raise ValueError(
                "EC2 Query Filter Invalid %s" % self.data)
        self.key = self.data.keys()[0]
        self.value = self.data.values()[0]

        if self.key not in EC2_VALID_FILTERS and not self.key.startswith(
                'tag:'):
            raise ValueError(
                "EC2 Query Filter invalid filter name %s" % (self.data))
                
        if self.value is None:
            raise ValueError(
                "EC2 Query Filters must have a value, use tag-key"
                " w/ tag name as value for tag present checks"
                " %s" % self.data)
        return self
    
    def query(self):
        value = self.value
        if isinstance(self.value, basestring):
            value = [self.value]
            
        return {'Name': self.key, 'Values': value}


    
                                    