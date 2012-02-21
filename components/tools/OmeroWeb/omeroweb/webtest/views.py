from django.http import HttpResponseRedirect, HttpResponse
from django.core.urlresolvers import reverse
from django.shortcuts import render_to_response
from omeroweb.webgateway.views import getBlitzConnection, _session_logout
from omeroweb.webgateway import views as webgateway_views
from omeroweb.webclient.views import isUserConnected
from omeroweb.webadmin.custom_models import Server

from webtest_utils import getSpimData
from cStringIO import StringIO

import settings
import logging
import traceback
import omero
from omero.rtypes import rint, rstring
import omero.gateway

logger = logging.getLogger(__name__)    


try:
    import Image
except: #pragma: nocover
    try:
        from PIL import Image
    except:
        logger.error('No PIL installed, line plots and split channel will fail!')


@isUserConnected    # wrapper handles login (or redirects to webclient login). Connection passed in **kwargs
def dataset(request, datasetId, **kwargs):
    """ 'Hello World' example from tutorial on http://trac.openmicroscopy.org.uk/ome/wiki/OmeroWeb """
    conn = kwargs['conn']
    ds = conn.getObject("Dataset", datasetId)     # before OMERO 4.3 this was conn.getDataset(datasetId)
    return render_to_response('webtest/dataset.html', {'dataset': ds})    # generate html from template


@isUserConnected    # wrapper handles login (or redirects to webclient login). Connection passed in **kwargs
def index(request, **kwargs):
    conn = kwargs['conn']
    return render_to_response('webtest/index.html', {'conn': conn})


@isUserConnected
def channel_overlay_viewer(request, imageId, **kwargs):
    """
    Viewer for overlaying separate channels from the same image or different images
    and adjusting horizontal and vertical alignment of each
    """
    conn = kwargs['conn']

    image = conn.getObject("Image", imageId)
    default_z = image.getSizeZ()/2
    
    # try to work out which channels should be 'red', 'green', 'blue' based on rendering settings
    red = None
    green = None
    blue = None
    notAssigned = []
    channels = []
    for i, c in enumerate(image.getChannels()):
        channels.append( {'name':c.getName()} )
        if c.getColor().getRGB() == (255, 0, 0) and red == None:
            red = i
        elif c.getColor().getRGB() == (0, 255, 0) and green == None:
            green = i
        elif c.getColor().getRGB() == (0, 0, 255) and blue == None:
            blue = i
        else: 
            notAssigned.append(i)
    # any not assigned - try assigning
    for i in notAssigned:
        if red == None: red = i
        elif green == None: green = i
        elif blue == None: blue = i
        
    # see if we have z, x, y offsets already annotated on this image
    # added by javascript in viewer. E.g. 0|z:1_x:0_y:0,1|z:0_x:10_y:0,2|z:0_x:0_y:0
    ns = "omero.web.channel_overlay.offsets"
    comment = image.getAnnotation(ns)
    if comment == None:     # maybe offset comment has been added manually (no ns)
        for ann in image.listAnnotations():
            if isinstance(ann, omero.gateway.CommentAnnotationWrapper):
                if ann.getValue().startswith("0|z:"):
                    comment = ann
                    break
    if comment != None:
        offsets = comment.getValue()
        for o in offsets.split(","):
            index,zxy = o.split("|",1)
            if int(index) < len(channels):
                keyVals = zxy.split("_")
                for kv in keyVals:
                    key, val = kv.split(":")
                    if key == "z": val = int(val) + default_z
                    channels[int(index)][key] = int(val)

    return render_to_response('webtest/demo_viewers/channel_overlay_viewer.html', {
        'image': image, 'channels':channels, 'default_z':default_z, 'red': red, 'green': green, 'blue': blue})


@isUserConnected
def render_channel_overlay (request, **kwargs):
    """
    Overlays separate channels (red, green, blue) from the same image or different images
    manipulating each indepdently (translate, scale, rotate etc? )
    """
    conn = kwargs['conn']

    # request holds info on all the planes we are working on and offset (may not all be visible)
    # planes=0|imageId:z:c:t$x:shift_y:shift_rot:etc,1|imageId...
    # E.g. planes=0|2305:7:0:0$x:-50_y:10,1|2305:7:1:0,2|2305:7:2:0&red=2&blue=0&green=1
    planes = {}
    p = request.REQUEST.get('planes', None)
    if p is None:
        return HttpResponse("Request needs plane info to render jpeg. E.g. ?planes=0|2305:7:0:0$x:-50_y:10,1|2305:7:1:0,2|2305:7:2:0&red=2&blue=0&green=1")
    for plane in p.split(','):
        infoMap = {}
        plane_info = plane.split('|')
        key = plane_info[0].strip()
        info = plane_info[1].strip()
        shift = None
        if info.find('$')>=0:
            info,shift = info.split('$')
        imageId,z,c,t = [int(i) for i in info.split(':')]
        infoMap['imageId'] = imageId
        infoMap['z'] = z
        infoMap['c'] = c
        infoMap['t'] = t
        if shift != None:
            for kv in shift.split("_"):
                k, v = kv.split(":")
                infoMap[k] = v
        planes[key] = infoMap

    # from the request we need to know which plane is blue, green, red (if any) by index
    # E.g. red=0&green=2
    red = request.REQUEST.get('red', None)
    green = request.REQUEST.get('green', None)
    blue = request.REQUEST.get('blue', None)

    # kinda like split-view: we want to get single-channel images...
    # red...
    redImg = None

    def translate(image, deltaX, deltaY):

        xsize, ysize = image.size
        mode = image.mode
        bg = Image.new(mode, image.size)
        x = abs(min(deltaX, 0))
        pasteX = max(0, deltaX)
        y = abs(min(deltaY, 0))
        pasteY = max(0, deltaY)

        part = image.crop((x, y, xsize-deltaX, ysize-deltaY))
        bg.paste(part, (pasteX, pasteY))
        return bg

    def getPlane(planeInfo):
        """ Returns the rendered plane split into a single channel (ready for merging) """
        img = conn.getObject("Image", planeInfo['imageId'])
        img.setActiveChannels((planeInfo['c']+1,))
        img.setGreyscaleRenderingModel()
        rgb = img.renderImage(planeInfo['z'], planeInfo['t'])

        # somehow this line is required to prevent an error at 'rgb.split()'
        rgb.save(StringIO(), 'jpeg', quality=90)

        r,g,b = rgb.split()  # go from RGB to L

        x,y = 0,0
        if 'x' in planeInfo:
            x = int(planeInfo['x'])
        if 'y' in planeInfo:
            y = int(planeInfo['y'])

        if x or y:
            r = translate(r, x, y)
        return r

    redChannel = None
    greenChannel = None
    blueChannel = None
    if red != None and red in planes:
        redChannel = getPlane(planes[red])
    if green != None and green in planes:
        greenChannel = getPlane(planes[green])
    if blue != None and blue in planes:
        blueChannel = getPlane(planes[blue])

    if redChannel != None:
        size = redChannel.size
    elif greenChannel != None:
        size = greenChannel.size
    elif blueChannel != None:
        size = blueChannel.size

    black = Image.new('L', size)
    redChannel = redChannel and redChannel or black
    greenChannel = greenChannel and greenChannel or black
    blueChannel = blueChannel and blueChannel or black

    merge = Image.merge("RGB", (redChannel, greenChannel, blueChannel))
    # convert from PIL back to string image data
    rv = StringIO()
    compression = 0.9
    merge.save(rv, 'jpeg', quality=int(compression*100))
    jpeg_data = rv.getvalue()

    rsp = HttpResponse(jpeg_data, mimetype='image/jpeg')
    return rsp


@isUserConnected
def add_annotations (request, **kwargs):
    """
    Creates a L{omero.gateway.CommentAnnotationWrapper} and adds it to the images according 
    to variables in the http request. 
    
    @param request:     The django L{django.core.handlers.wsgi.WSGIRequest}
                            - imageIds:     A comma-delimited list of image IDs
                            - comment:      The text to add as a comment to the images
                            - ns:           Namespace for the annotation
                            - replace:      If "true", try to replace existing annotation with same ns
                            
    @return:            A simple html page with a success message 
    """
    
    conn = kwargs['conn']
    
    idList = request.REQUEST.get('imageIds', None)    # comma - delimited list
    if idList:
        imageIds = [long(i) for i in idList.split(",")]
    else: imageIds = []
    
    comment = request.REQUEST.get('comment', None)
    ns = request.REQUEST.get('ns', None)
    replace = request.REQUEST.get('replace', False) in ('true', 'True')
    
    updateService = conn.getUpdateService()
    ann = omero.model.CommentAnnotationI()
    ann.setTextValue(rstring( str(comment) ))
    if ns != None:
        ann.setNs(rstring( str(ns) ))
    ann = updateService.saveAndReturnObject(ann)
    annId = ann.getId().getValue()
    
    images = []
    for iId in imageIds:
        image = conn.getObject("Image", iId)
        if image == None: continue
        if replace and ns != None:
            oldComment = image.getAnnotation(ns)
            if oldComment != None:
                oldComment.setTextValue(rstring( str(comment) ))
                updateService.saveObject(oldComment)
                continue
        l = omero.model.ImageAnnotationLinkI()
        parent = omero.model.ImageI(iId, False)     # use unloaded object to avoid update conflicts
        l.setParent(parent)
        l.setChild(ann)
        updateService.saveObject(l)
        images.append(image)
        
    return render_to_response('webtest/util/add_annotations.html', {'images':images, 'comment':comment})
    

@isUserConnected
def split_view_figure (request, **kwargs):
    """
    Generates an html page displaying a number of images in a grid with channels split into different columns. 
    The page also includes a form for modifying various display parameters and re-submitting
    to regenerate this page. 
    If no 'imageIds' parameter (comma-delimited list) is found in the 'request', the page generated is simply 
    a form requesting image IDs. 
    If there are imageIds, the first ID (image) is used to generate the form based on channels of that image.
    
    @param request:     The django L{http request <django.core.handlers.wsgi.WSGIRequest>}
    
    @return:            The http response - html page displaying split view figure.  
    """
    
    conn = kwargs['conn']
    
    query_string = request.META["QUERY_STRING"]
    
    
    idList = request.REQUEST.get('imageIds', None)    # comma - delimited list
    if idList:
        imageIds = [long(i) for i in idList.split(",")]
    else:
        imageIds = []
    
    split_grey = request.REQUEST.get('split_grey', None)
    merged_names = request.REQUEST.get('merged_names', None)
    proj = request.REQUEST.get('proj', "normal")    # intmean, intmax, normal
    try:
        w = request.REQUEST.get('width', 0)
        width = int(w)
    except:
        width = 0
    try:
        h = request.REQUEST.get('height', 0)
        height = int(h)
    except:
        height = 0
        
    # returns a list of channel info from the image, overridden if values in request
    def getChannelData(image):
        channels = []
        i = 0;
        for i, c in enumerate(image.getChannels()):
            name = request.REQUEST.get('cName%s' % i, c.getLogicalChannel().getName())
            # if we have channel info from a form, we know that checkbox:None is unchecked (not absent)
            if request.REQUEST.get('cName%s' % i, None):
                active = (None != request.REQUEST.get('cActive%s' % i, None) )
                merged = (None != request.REQUEST.get('cMerged%s' % i, None) )
            else:
                active = True
                merged = True
            colour = c.getColor().getHtml()
            start = request.REQUEST.get('cStart%s' % i, c.getWindowStart())
            end = request.REQUEST.get('cEnd%s' % i, c.getWindowEnd())
            render_all = (None != request.REQUEST.get('cRenderAll%s' % i, None) )
            channels.append({"name": name, "index": i, "active": active, "merged": merged, "colour": colour, 
                "start": start, "end": end, "render_all": render_all})
        return channels
    
    channels = None
    images = []
    for iId in imageIds:
        image = conn.getObject("Image", iId)
        if image == None: continue
        default_z = image.getSizeZ()/2   # image.getZ() returns 0 - should return default Z? 
        # need z for render_image even if we're projecting
        images.append({"id":iId, "z":default_z, "name": image.getName() })
        if channels == None:
            channels = getChannelData(image)
        if height == 0:
            height = image.getSizeY()
        if width == 0:
            width = image.getSizeX()
    
    size = {"height": height, "width": width}
    c_strs = []
    if channels:    # channels will be none when page first loads (no images)
        indexes = range(1, len(channels)+1)
        c_string = ",".join(["-%s" % str(c) for c in indexes])     # E.g. -1,-2,-3,-4
        mergedFlags = []
        for i, c, in enumerate(channels):
            if c["render_all"]:
                levels = "%s:%s" % (c["start"], c["end"])
            else: levels = ""
            if c["active"]:
                onFlag = str(i+1) + "|"
                onFlag += levels
                if split_grey: onFlag += "$FFFFFF"  # E.g.   1|100:505$0000FF
                c_strs.append( c_string.replace("-%s" % str(i+1), onFlag) )  # E.g. 1,-2,-3  or  1|$FFFFFF,-2,-3
            if c["merged"]:
                mergedFlags.append("%s|%s" % (i+1, levels))     # E.g. '1|200:4000'
            else: mergedFlags.append("-%s" % (i+1))  # E.g. '-1'
        # turn merged channels on in the last image
        c_strs.append( ",".join(mergedFlags) )
    
    return render_to_response('webtest/demo_viewers/split_view_figure.html', {'images':images, 'c_strs': c_strs,'imageIds':idList,
        'channels': channels, 'split_grey':split_grey, 'merged_names': merged_names, 'proj': proj, 'size': size, 'query_string':query_string})


@isUserConnected
def dataset_split_view (request, datasetId, **kwargs):
    """
    Generates a web page that displays a dataset in two panels, with the option to choose different
    rendering settings (channels on/off) for each panel. It uses the render_image url for each
    image, generating the full sized image which is scaled down to view. 
    
    The page also includes a form for editing the channel settings and display size of images.
    This form resubmits to this page and displays the page again with updated parameters. 
    
    @param request:     The django L{http request <django.core.handlers.wsgi.WSGIRequest>}
    @param datasetId:   The ID of the dataset. 
    @type datasetId:    Number. 
    
    @return:            The http response - html page displaying split view figure.
    """
    
    conn = kwargs['conn']
        
    dataset = conn.getObject("Dataset", datasetId)
    
    try:
        w = request.REQUEST.get('width', 100)
        width = int(w)
    except:
        width = 100
    try:
        h = request.REQUEST.get('height', 100)
        height = int(h)
    except:
        height = 100
        
    # returns a list of channel info from the image, overridden if values in request
    def getChannelData(image):
        channels = []
        i = 0;
        for i, c in enumerate(image.getChannels()):
            name = c.getLogicalChannel().getName()
            # if we have channel info from a form, we know that checkbox:None is unchecked (not absent)
            if request.REQUEST.get('cStart%s' % i, None):
                active_left = (None != request.REQUEST.get('cActiveLeft%s' % i, None) )
                active_right = (None != request.REQUEST.get('cActiveRight%s' % i, None) )
            else:
                active_left = True
                active_right = True
            colour = c.getColor().getHtml()
            start = request.REQUEST.get('cStart%s' % i, c.getWindowStart())
            end = request.REQUEST.get('cEnd%s' % i, c.getWindowEnd())
            render_all = (None != request.REQUEST.get('cRenderAll%s' % i, None) )
            channels.append({"name": name, "index": i, "active_left": active_left, "active_right": active_right, 
                "colour": colour, "start": start, "end": end, "render_all": render_all})
        return channels
        
    images = []
    channels = None
    
    for image in dataset.listChildren():
        if channels == None:
            channels = getChannelData(image)
        default_z = image.getSizeZ()/2   # image.getZ() returns 0 - should return default Z? 
        # need z for render_image even if we're projecting
        images.append({"id":image.getId(), "z":default_z, "name": image.getName() })
    
    size = {'width':width, 'height':height}
    
    indexes = range(1, len(channels)+1)
    c_string = ",".join(["-%s" % str(c) for c in indexes])     # E.g. -1,-2,-3,-4

    leftFlags = []
    rightFlags = []
    for i, c, in enumerate(channels):
        if c["render_all"]:
            levels = "%s:%s" % (c["start"], c["end"])
        else: levels = ""
        if c["active_left"]:
            leftFlags.append("%s|%s" % (i+1, levels))     # E.g. '1|200:4000'
        else: leftFlags.append("-%s" % (i+1))  # E.g. '-1'
        if c["active_right"]:
            rightFlags.append("%s|%s" % (i+1, levels))     # E.g. '1|200:4000'
        else: rightFlags.append("-%s" % (i+1))  # E.g. '-1'
    
    c_left = ",".join(leftFlags)
    c_right = ",".join(rightFlags)
    
    return render_to_response('webtest/demo_viewers/dataset_split_view.html', {'dataset': dataset, 'images': images, 
        'channels':channels, 'size': size, 'c_left': c_left, 'c_right': c_right})


@isUserConnected
def image_dimensions (request, imageId, **kwargs):
    """
    Prepare data to display various dimensions of a multi-dim image as axes of a grid of image planes. 
    E.g. x-axis = Time, y-axis = Channel. 
    If the image has spim data, then combine images with different SPIM angles to provide an additional
    dimension. Also get the SPIM data from various XML annotations and display on page. 
    """
        
    conn = kwargs['conn']
    
    image = conn.getObject("Image", imageId)
    if image is None:
        return render_to_response('webtest/demo_viewers/image_dimensions.html', {}) 
    
    mode = request.REQUEST.get('mode', None) and 'g' or 'c'
    dims = {'Z':image.getSizeZ(), 'C': image.getSizeC(), 'T': image.getSizeT()}
    
    default_yDim = 'Z'
    
    spim_data = getSpimData(conn, image)
    if spim_data is not None:
        dims['Angle'] = len(spim_data['images'])
        default_yDim = 'Angle'
    
    xDim = request.REQUEST.get('xDim', 'T')
    if xDim not in dims.keys():
        xDim = 'T'
        
    yDim = request.REQUEST.get('yDim', default_yDim)
    if yDim not in dims.keys():
        yDim = 'Z'
    
    xFrames = int(request.REQUEST.get('xFrames', 5))
    xSize = dims[xDim]
    yFrames = int(request.REQUEST.get('yFrames', 5))
    ySize = dims[yDim]
    
    xFrames = min(xFrames, xSize)
    yFrames = min(yFrames, ySize)
    
    xRange = range(xFrames)
    yRange = range(yFrames)
    
    # 2D array of (theZ, theC, theT)
    grid = []
    for y in yRange:
        grid.append([])
        for x in xRange:
            iid, theZ, theC, theT = image.id, 0,None,0
            if xDim == 'Z':
                theZ = x
            if xDim == 'C':
                theC = x
            if xDim == 'T':
                theT = x
            if xDim == 'Angle':
                iid = spim_data['images'][x].id
            if yDim == 'Z':
                theZ = y
            if yDim == 'C':
                theC = y
            if yDim == 'T':
                theT = y
            if yDim == 'Angle':
                iid = spim_data['images'][y].id
                
            grid[y].append( (iid, theZ, theC is not None and theC+1 or None, theT) )
    
        
    size = {"height": 125, "width": 125}
    
    return render_to_response('webtest/demo_viewers/image_dimensions.html', {'image':image, 'spim_data':spim_data, 'grid': grid, 
        "size": size, "mode":mode, 'xDim':xDim, 'xRange':xRange, 'yRange':yRange, 'yDim':yDim, 
        'xFrames':xFrames, 'yFrames':yFrames})


def common_templates (request, base_template):
    """ Simply return the named template. Similar functionality to django.views.generic.simple.direct_to_template """
    template_name = 'webtest/common/%s.html' % base_template
    from django.template import RequestContext
    return render_to_response(template_name, context_instance=RequestContext(request))
