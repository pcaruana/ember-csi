# Copyright (c) 2018, Red Hat, Inc.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

from __future__ import absolute_import
from builtins import int
import os
import stat

from google.protobuf import timestamp_pb2
import grpc

from ember_csi import base
from ember_csi import common
from ember_csi import config
from ember_csi import constants
from ember_csi.v1_0_0 import csi_pb2_grpc as csi
from ember_csi.v1_0_0 import csi_types as types


CONF = config.CONF


class Controller(base.TopologyBase, base.SnapshotBase, base.ControllerBase):
    CSI = csi
    TYPES = types
    DELETE_SNAP_RESP = types.DeleteSnapResp()
    CTRL_CAPABILITIES = (types.CtrlCapabilityType.CREATE_DELETE_VOLUME,
                         types.CtrlCapabilityType.PUBLISH_UNPUBLISH_VOLUME,
                         types.CtrlCapabilityType.LIST_VOLUMES,
                         types.CtrlCapabilityType.GET_CAPACITY,
                         types.CtrlCapabilityType.CREATE_DELETE_SNAPSHOT,
                         types.CtrlCapabilityType.LIST_SNAPSHOTS,
                         types.CtrlCapabilityType.CLONE_VOLUME,
                         types.CtrlCapabilityType.PUBLISH_READONLY,
                         )

    def __init__(self, server, persistence_config, backend_config,
                 ember_config=None, **kwargs):
        self._init_topology(types.ServiceType.VOLUME_ACCESSIBILITY_CONSTRAINTS)
        super(Controller, self).__init__(server, persistence_config,
                                         backend_config, ember_config,
                                         **kwargs)

    # CreateVolume implemented on base Controller class which requires
    # _validate_requirements, _create_volume, and _convert_volume_type
    # methods.
    def _validate_requirements(self, request, context):
        super(Controller, self)._validate_requirements(request, context)
        self._validate_accessibility(request, context)

    def _create_from_vol(self, vol_id, vol_size, name, context, **params):
        src_vol = self._get_vol(volume_id=vol_id)

        if not src_vol:
            context.abort(grpc.StatusCode.NOT_FOUND,
                          'Volume %s does not exist' % vol_id)
        if src_vol.status not in ('available', 'in-use'):
            context.abort(grpc.StatusCode.INVALID_ARGUMENT,
                          'Volume %s is not available' % vol_id)
        if src_vol.size > vol_size:
            context.abort(grpc.StatusCode.OUT_OF_RANGE,
                          'Volume %s is bigger than requested volume' % vol_id)
        vol = src_vol.clone(name=name, size=vol_size, **params)
        return vol

    def _create_volume(self, name, vol_size, request, context, **params):
        if not request.HasField('volume_content_source'):
            return super(Controller, self)._create_volume(name, vol_size,
                                                          request, context,
                                                          **params)
        # Check size
        source = request.volume_content_source
        if source.HasField('snapshot'):
            vol = self._create_from_snap(source.snapshot.snapshot_id, vol_size,
                                         request.name, context, **params)

        else:
            vol = self._create_from_vol(source.volume.volume_id, vol_size,
                                        request.name, context, **params)
        return vol

    def _convert_volume_type(self, vol):
        specs = vol.volume_type.extra_specs if vol.volume_type_id else None
        parameters = dict(capacity_bytes=int(vol.size * constants.GB),
                          volume_id=vol.id,
                          volume_context=specs)

        # If we pass the request we could do
        # if not request.HasField('volume_content_source'):
        #    parameters['content_source'] = request.content_source
        if vol.source_volid:
            parameters['content_source'] = types.VolumeContentSource(
                volume=types.VolumeSource(volume_id=vol.source_vol_id))

        elif vol.snapshot_id:
            parameters['content_source'] = types.VolumeContentSource(
                snapshot=types.SnapshotSource(snapshot_id=vol.snapshot_id))

        # accessible_topology should only be returned if we reported
        # VOLUME_ACCESSIBILITY_CONSTRAINTS capability.
        if self.GRPC_TOPOLOGIES:
            parameters['accessible_topology'] = self.GRPC_TOPOLOGIES

        return types.Volume(**parameters)

    # DeleteVolume implemented on base Controller class

    # ControllerPublishVolume implemented on base Controller class.

    # ControllerUnpublishVolume implemented on base Controller class.

    @common.debuggable
    @common.logrpc
    @common.require('volume_id', 'volume_capabilities')
    def ValidateVolumeCapabilities(self, request, context):
        vol = self._get_vol(request.volume_id)
        if not vol:
            context.abort(grpc.StatusCode.NOT_FOUND,
                          'Volume %s does not exist' % request.volume_id)

        message = self._validate_capabilities(request.volume_capabilities)
        if message:
            return types.ValidateResp(message=message)

        if request.parameters:
            for k, v in request.parameters.items():
                v2 = request.volume_context.get(k)
                if v != v2:
                    message = 'Parameter %s does not match' % k
                    return types.ValidateResp(message=message)

        confirmed = types.ValidateResp.Confirmed(
            volume_context=request.volume_context,
            volume_capabilities=request.volume_capabilities,
            parameters=request.parameters)
        return types.ValidateResp(confirmed=confirmed)

    # ListVolumes implemented on base Controller class
    # Requires _convert_volume_type method.

    # GetCapacity implemented on base Controller class which requires
    # _validate_requirements

    # ControllerGetCapabilities implemented on base Controller class using the
    # CTRL_CAPABILITIES attribute.

    def _convert_snapshot_type(self, snap):
        creation_time = timestamp_pb2.Timestamp()
        created_at = snap.created_at.replace(tzinfo=None)
        creation_time.FromDatetime(created_at)

        snapshot = types.Snapshot(
            snapshot_id=snap.id,
            source_volume_id=snap.volume_id,
            creation_time=creation_time,
            ready_to_use=True)
        return snapshot


class Node(base.NodeBase):
    CSI = csi
    TYPES = types
    NODE_CAPABILITIES = (types.NodeCapabilityType.STAGE_UNSTAGE_VOLUME,
                         types.NodeCapabilityType.GET_VOLUME_STATS)
    NODE_TOPOLOGY = None

    def __init__(self, server, persistence_config=None, ember_config=None,
                 node_id=None, storage_nw_ip=None, **kwargs):
        # TODO(geguileo): Report max_volumes_per_node based on driver
        topo_capab = self.TYPES.ServiceType.VOLUME_ACCESSIBILITY_CONSTRAINTS
        if CONF.NODE_TOPOLOGY:
            if topo_capab not in self.PLUGIN_CAPABILITIES:
                self.PLUGIN_CAPABILITIES.append(topo_capab)
            self.NODE_TOPOLOGY = self.TYPES.Topology(
                segments=CONF.NODE_TOPOLOGY)
        super(Node, self).__init__(server, persistence_config, ember_config,
                                   node_id, storage_nw_ip, **kwargs)
        params = dict(node_id=node_id)
        if self.NODE_TOPOLOGY:
            params['accessible_topology'] = self.NODE_TOPOLOGY
        self.node_info_resp = types.NodeInfoResp(**params)

    # NodeStageVolume implemented on base Controller class
    # NodeUnstageVolume implemented on base Controller class
    # NodePublishVolume implemented on base Controller class
    # NodeUnpublishVolume implemented on base Controller class

    # TODO(geguileo): Implement NodeGetVolumeStats
    @common.debuggable
    @common.logrpc
    @common.require('volume_id', 'volume_path')
    @common.Worker.unique('volume_id')
    def NodeGetVolumeStats(self, request, context):
        path = request.volume_path
        try:
            st_mode = os.stat(path).st_mode
        except OSError:
            context.abort(grpc.StatusCode.NOT_FOUND,
                          'Cannot access path %s' % path)

        device_for_path = self._get_device(path)
        device_for_vol, private_bind = self._get_vol_device(request.volume_id)

        if not device_for_vol or device_for_path != private_bind:
            context.abort(grpc.StatusCode.NOT_FOUND,
                          'Path does not match with requested volume')

        if stat.S_ISDIR(st_mode):
            stats = os.statvfs(path)
            size = stats.f_frsize * stats.f_blocks
            available = stats.f_frsize * stats.f_bavail
            used = size - available

        else:  # is block
            size_name = os.path.join('/sys/class/block',
                                     os.path.basename(device_for_vol), 'size')
            with open(size_name) as f:
                blocks = int(f.read())
            size = 512 * blocks
            used = available = None

        return types.VolumeStatsResp(usage=[types.VolumeUsage(
            unit=types.UsageUnit.BYTES,
            total=size,
            used=used,
            available=available)])

    # NodeGetCapabilities implemented on base Controller class using
    # NODE_CAPABILITIES attribute.

    @common.debuggable
    @common.logrpc
    def NodeGetInfo(self, request, context):
        return self.node_info_resp


class All(Controller, Node):
    def __init__(self, server, persistence_config, backend_config,
                 ember_config=None, node_id=None, storage_nw_ip=None):
        Controller.__init__(self, server,
                            persistence_config=persistence_config,
                            backend_config=backend_config,
                            ember_config=ember_config)
        Node.__init__(self, server, node_id=node_id,
                      storage_nw_ip=storage_nw_ip)
