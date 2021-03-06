#------------------------------------
#
# reading the ICDB resource table(s)
# pull out records in a saved selection set
# and find each Primary#'s location by it's APN in the given parcel layer
#
# 2016-05-26:
#	rolled out to NWIC use
#
# 2016-09-09:
#	add capability to map reports by APN
#	and add pattern matching to detect/report potentially mal-formed APN values
#
# 2017-02-24:
#	Chuck discovers that for reports with multiple APN values, that only the last APN is getting mapped
#	fix the multiple-APN-valued reports/records
#	implement a system to clear any pre-existing selections only once per run_date
#	fix the output chatter to better reflect what APN's are found in the parcel layers
#	parallel the fixes for the mapping resources, too.
#
# 2018-03-06
#	update to get icdb connection parameters from the system environment variables
#		(see corresponding versions of PrepBatchTool.py)
#
# 2019-05-23
#	need to test what the TrinNo value is in the tblResource record in ICDB
#		only copy it over to the output shape field if it's non-zero
#		because copying over whatever the dictionary reference returns is putting a lot of '0' values in TrinNo attribute in GIS
#		and we need to stop doing that!
#	based on: https://gis.stackexchange.com/questions/122780/how-to-allow-null-values-in-a-feature-layer
#		the problem is that this code outputs a shape file. and shapefiles don't allow Null values (at least for INT fields)
#		the correct solution is to write the output to a poly feature in a file geodatabase... Ugh.


import openpyxl
import pymssql
import arcpy
import getpass
import datetime
import os
import string
import re

DBsaved_selection_file =		arcpy.GetParameterAsText(0)									# input file path of saved selections

map_by_APN_gdb = "mapByAPN.gdb"					# filename of the geodatabase to create/use for output

run_user = getpass.getuser()					# get the current user's login id
run_date = datetime.datetime(datetime.datetime.now().year,datetime.datetime.now().month,datetime.datetime.now().day)	# used for DigDate

#-----
# derive an output .shp filename based on the saved selection file name....
#	'x' is an input path/file to use as a stem for the output file location and filename
#	'stem' is a short string to add to the filename as a way of identifying the output
#	'ext' is the file type/extension to use
def make_output_file(x,stem,ext):
	(x_path, x_file) = os.path.split(x)			# split apart into <Drive:/path> <filename.extension>
	(x_base, x_ext) = os.path.splitext(x_file)	# split apart into <Drive:/path/filename> <extension>
	remove_letters = u"~`!@#$%^&*()+-={}[]|\\:;<>?/.,\""		# list of characters to convert to '_'
	trans = dict((ord(char),u'_') for char in remove_letters)	# f-ing UniCode #*%^@&!!
	t = ""										# do our own translate loop
	#arcpy.AddMessage("x_base = {0}".format(x_base))
	for c in x_base:
		if c in remove_letters:
			t += "_"
		else:
			t += c
	#x_base.translate(trans)
	#arcpy.AddMessage("t = {0}".format(t))
	#t_path = os.path.join(x_path,t+x_ext)
	#first_try = "{0}_{1}.{2}".format(t_path,stem,ext)
	first_try = os.path.join(x_path,"{0}_{1}.{2}".format(t,stem,ext))
	if not os.path.exists(first_try):			# see if the un-numbered output file can work
		return first_try
	seq = 1								# start trying sequential numbering
	while (1):
		#seq_try = "{0}_{1}_{2}.{3}".format(t_path,stem,seq,ext)
		seq_try = os.path.join(x_path,"{0}_{1}_{2}.{3}".format(t,stem,seq,ext))
		if not os.path.exists(seq_try):
			return seq_try
		seq += 1						# bump up and loop around to try the next one

#--------------------
# create output feature
#
#	creates a file geodatabase in the directory
#	creates a feature in the file geodatabase
#	returns (result,featurename)
#		if result=True, then featurename contains the pathname to the featureclass
#		if result=False, then featurename contains an error msg string

def create_output_feature(base_file,template_name):
	(source_dir, source_file) = os.path.split(base_file)					# split apart the directory path from the filename
	(source_basename, source_ext) = os.path.splitext(source_file)			# split the extension off the filename
	map_by_apn_fgdb_path = os.path.join(source_dir,map_by_APN_gdb)			# path to fgdb
	#arcpy.AddMessage(map_by_apn_fgdb_path)
	if not arcpy.Exists(map_by_apn_fgdb_path):
		arcpy.AddMessage("creating geodatabase for Map_by_APN outputs: {0}".format(map_by_apn_fgdb_path))
		arcpy_result = arcpy.CreateFileGDB_management(source_dir,map_by_APN_gdb)
		if arcpy_result.status != 4:										# 4 is the 'success' status result
			return(False,"; ".join(arcpy_result.getMessages()))				# return fail and errors
	# at this point the output geodatabase exists
	# now, find the next available feature class name
	remove_letters = u" ~`!@#$%^&*()+-={}[]|\\:;<>?/.,\""					# list of characters to convert to '_'
	new_basename = ""														# do our own translate loop (f-cking Unicode)
	for c in source_basename:
		if c in remove_letters:
			new_basename += "_"
		else:
			new_basename += c
	#now, the basename is cleaned up, start searching for the next sequential feature
	seq = 1
	while (True):
		try_feature = new_basename + "_APN_{0}".format(seq)
		try_path = os.path.join(map_by_apn_fgdb_path,try_feature)
		if not arcpy.Exists(try_path):
			break
		seq += 1
	#arcpy.AddMessage("try to create {0}".format(try_feature))
	arcpy_result = arcpy.CreateFeatureclass_management(map_by_apn_fgdb_path,try_feature,"POLYGON",template_name,"SAME_AS_TEMPLATE","SAME_AS_TEMPLATE",template_name)
	if arcpy_result.status != 4:
		return(False,"; ".join(arcpy_result.getMessages()))					# return fail and errors
	return(True, arcpy_result.getOutput(0))									# it all worked, return True and the path to the feature class

			

#--------------------
# start a connect to the ICDB
# by pullin connection values from the environment
#
def connect_to_icdb():
	icdb_sqlserv = os.getenv("ICDB_sqlserv")
	icdb_sqlport = os.getenv("ICDB_sqlport")
	icdb_sqldb   = os.getenv("ICDB_sqldb")
	if icdb_sqlport:
		db_connect = pymssql.connect(server=icdb_sqlserv,port=icdb_sqlport,database=icdb_sqldb)
	else:
		db_connect = pymssql.connect(server=icdb_sqlserv,database=icdb_sqldb)
	return db_connect
	
#-------
# valid sheet names accepted (these are generated by the DB Save... function
# we check for these as a sort validation that the spreadsheet we're looking is 
# in some expected form
sheet_saved_resources		= "tblResSelect"
sheet_saved_reports			= "tblInvSelect"

parcel_layers = {
	 1	: r'Basemap\Parcels\ALA_APN',
	 6	: r'Basemap\Parcels\COL_APN',
	 7	: r'Basemap\Parcels\CCO_APN',
	 8	: r'Basemap\Parcels\DNO_APN',
	12	: r'Basemap\Parcels\HUM_APN',
	17	: r'Basemap\Parcels\LAK_APN',
	21	: r'Basemap\Parcels\MRN_APN',
	23	: r'Basemap\Parcels\MEN_APN',
	27	: r'Basemap\Parcels\MNT_APN',
	28	: r'Basemap\Parcels\NAP_APN',
	35	: r'Basemap\Parcels\SBN_APN',
	38	: r'Basemap\Parcels\SFR_APN',
	41	: r'Basemap\Parcels\SMA_APN',
	43	: r'Basemap\Parcels\SCL_APN',
	44	: r'Basemap\Parcels\SCR_APN',
	48	: r'Basemap\Parcels\SOL_APN',
	49	: r'Basemap\Parcels\SON_APN',
	57	: r'Basemap\Parcels\YOL_APN'
}

parcel_selections_cleared = {
	 1	: False,
	 6	: False,
	 7	: False,
	 8	: False,
	12	: False,
	17	: False,
	21	: False,
	23	: False,
	27	: False,
	28	: False,
	35	: False,
	38	: False,
	41	: False,
	43	: False,
	44	: False,
	48	: False,
	49	: False,
	57	: False
}

county_names = {
	"Alameda"		:	1,
	"Colusa"		:	6,
	"Contra Costa"	:	7,
	"Del Norte"		:	8,
	"Humboldt"		:	12,
	"Lake"			:	17,
	"Marin"			:	21,
	"Mendocino"		:	23,
	"Monterey"		:	27,
	"Napa"			:	28,
	"San Benito"	:	35,
	"San Francisco"	:	38,
	"San Mateo"		:	41,
	"Santa Clara"	:	43,
	"Santa Cruz"	:	44,
	"Solano"		:	48,
	"Sonoma"		:	49,
	"Yolo"			:	57
}

county_numbers = { county_names[x]:x for x in county_names}

# a table of APN patterns, by county
# a table of regular expressions that can be used to attempt to detect mis-formed APN values in the ICDB
apn_patterns = {
	1	:	"^\d{1,3}[A-Z]?-\d{1,4}-\d{1,3}(-\d{1,2})?$",
	6	:	"^\d{3}-\d{3}-d{3}$",
	7	:	"^\d{3}-\d{3}-d{3}-000$",
	8	:	"^\d{3}-\d{3}-\d{2}$",
	12	:	"^\d{3}-\d{3}-\d{2}$",
	17	:	"^\d{3}-\d{3}-\d{2}$",
	21	:	"^\d{3}-\d{3}-\d{2}$",
	23	:	"^\d{3}-\d{3}-\d{2}$",
	27	:	"^\d{3}-\d{3}-\d{3}$",
	28	:	"^\d{3}-\d{3}-\d{2}$",
	35	:	"^\d{3}-\d{3}-\d{3}$",
	38	:	"^\d{4}-\d{3}(-[A-Z])?$",
	41	:	"^\d{3}-\d{3}-\d{3}$",
	43	:	"^\d{3}-\d{2}-\d{3}$",
	44	:	"^\d{3}-\d{3}-\d{2}$",
	48	:	"^\d{3}-\d{3}-\d{3}$",
	49	:	"^\d{3}-\d{3}-\d{3}$",
	57	:	"^\d{3}-\d{3}-\d{2}$"
}

def map_reports():
	arcpy.AddMessage("Mapping reports by APN")
	# loop through each S-#
	
	# create the output feature
	out_template = r"MAIN\Reports\Reports (polygons)"
	(success, output_shapefile_name) = create_output_feature(DBsaved_selection_file,out_template)
	if not success:
		arcpy.AddError(output_shapefile_name)
		return

	# create the output shapefile
	#output_shapefile_name = make_output_file(DBsaved_selection_file,"parcelmapped","shp")
	#arcpy.AddMessage("output to: {0}".format(output_shapefile_name))
	#(out_path, out_name) = os.path.split(output_shapefile_name)
	#out_template = r"MAIN\Reports\Reports (polygons)"
	#arcpy.CreateFeatureclass_management(out_path,out_name,"POLYGON",out_template,"SAME_AS_TEMPLATE","SAME_AS_TEMPLATE",out_template)
	shp_shape_field = arcpy.Describe(output_shapefile_name).ShapeFieldName
	
	count_voids = 0			# count S-#'s marked "Void"
	count_noCounty = 0		# count S-#'s with no county specified
	count_malformed = 0		# count reports with malformed APN's
	count_noAPN = 0			# count number of reports with no APN in ICDB
	count_noParcel = 0		# count number of reports with 0 APN found in parcel layer
	count_multiParcel = 0	# count number of reports with >1 APN found in parcel layer
	count_shapes = 0		# count number of parcel shapes output
	
	for s_no in DocList:
		current_report = "S-{0:06}".format(s_no)
		arcpy.AddMessage("{0}:".format(current_report))

		#----
		# start a connection and cursor to the ICDB
		icdb_connect = connect_to_icdb()
		icdb_cursor = icdb_connect.cursor(as_dict=True)

		#-----
		# get the parent table entry for this resource
		icdb_cursor.execute("Select * from tblInventory WHERE DocNo = {0}".format(s_no))
		icdb_report_parent = icdb_cursor.next()	# we'll assume that there's only 1 row returned
		#-----
		# skip this if it's marked "VOIDED" in the ICDB
		if icdb_report_parent['Voided']:
			arcpy.AddMessage("     {0} is marked VOIDED".format(current_report))
			count_voids += 1
			continue
		#----
		# extract some field values 
		if icdb_report_parent['CitTitle']:
			icdb_report_name = icdb_report_parent['CitTitle']
		else:
			icdb_report_name = "[none]"
		
		#-----
		# find all the report's address records
		icdb_cursor.execute("Select * from tblInventoryAddr WHERE DocNo = {0}".format(s_no))
		icdb_report_apns = []		# gather up the APN values found
		for icdb_addr in icdb_cursor:
			if icdb_addr['APN']:
				apn_cleaned = icdb_addr['APN'].strip()	# remove leading/trailing whitespace
				if (len(apn_cleaned) > 0) and (apn_cleaned not in icdb_report_apns):
					icdb_report_apns.append(apn_cleaned)
		arcpy.AddMessage("     {0} APN's found in ICDB".format(len(icdb_report_apns)))
		if len(icdb_report_apns) == 0:
			count_noAPN += 1				# count reports with no APN value
			continue						# skip this one
		
		#------
		# loop through the APN's found for this Report
		# this gets a bit tricky... the parcel APN layers are messy.
		# So, while 1 Report may be reasonably mapped to more than 1 APN
		# it is also possible that for a given APN, the parcel layer may have multiple shapes with that APN value
		# BUT... report identifiers (the S-#'s) don't have county numbers coded in them...
		# so... I guess we'll pull which county parcel layer to search by looking up the report's CountyName in tblInventoryCnty
		#	hopefully, there's only 1 county, or else we'll have to search *all* the counties listed for that APN # !?!?!?!?!
		
		icdb_report_counties = []					# make a list of the county numbers
		icdb_cursor.execute("Select * from tblInventoryCnty WHERE DocNo = {0}".format(s_no))
		for icdb_cnty in icdb_cursor:
			if icdb_cnty["CountyName"]:
				icdb_report_counties.append(county_names[icdb_cnty["CountyName"]])	# lookup the name and store county number
		if len(icdb_report_counties) < 1:
			count_noCounty += 1
			continue								# skip to next report (can't proceed without knowing which county to search)
		
		# for each county, search its APN table for all the APN's listed in the report's icdb (hopefully, this is only 1 county)
		# this is not a clean loop, looking up each APN in each county doesn't make sense, but the data are structured that way, alas
		this_report_malformed_apn = False
		apn_shapes = []											# store up tuples of (county,apn,shape) here
		for report_county in icdb_report_counties:
			# gather up all the parcel shapes we find in all counties
			parcel_layer = parcel_layers[report_county]			# get the layer name of the parcel
			if not parcel_selections_cleared[report_county]:
				arcpy.SelectLayerByAttribute_management(parcel_layer,"CLEAR_SELECTION")
				parcel_selections_cleared[report_county] = True
			for icdb_apn in icdb_report_apns:
				cursor_apn = arcpy.da.SearchCursor(parcel_layer,["APN","SHAPE@"],"APN = '{0}'".format(icdb_apn))
				# attempt to test the APN for well-formed-ness and output a blurb if not
				if not re.match(apn_patterns[report_county],icdb_apn):
					arcpy.AddMessage("      APN '{0}' in {1} county may not be well-formed".format(icdb_apn,county_numbers[report_county]))
					this_report_malformed_apn = True
				icdb_apn_count = 0
				for apn in cursor_apn:
					apn_shapes.append((report_county,icdb_apn,cursor_apn[1]))			# the SHAPE@ field is [1]
					icdb_apn_count += 1													# count the parcels with this APN
				arcpy.AddMessage("     searching for APN {0} in {1} : found {2} parcels".format(icdb_apn,parcel_layer,icdb_apn_count))
		
		if this_report_malformed_apn:
			count_malformed += 1						# count reports with mal-formed APN values
			
		if len(apn_shapes) > 0:							# if any shapes found...
			if len(apn_shapes) > 1:						# tally up the number of times that multiple parcels are found
				count_multiParcel += 1
			#-----
			# generate a feature in the output for each APN shape
			shp_to = arcpy.InsertCursor(output_shapefile_name)
			for apn_shape in apn_shapes:
				shp_new_row = shp_to.newRow()							# create empty record
				shp_new_row.setValue(shp_shape_field,apn_shape[2])		# the shape geometry
				shp_new_row.setValue('DocCo',apn_shape[0])				# the county number (saved in above loop)
				shp_new_row.setValue('DocNo',s_no)
				shp_new_row.setValue('OtherID',icdb_report_name)
				shp_new_row.setValue('DocSource','p')					# selects 'parcel (APN)'
				shp_new_row.setValue('DigSource','p')					# selects 'parcel (APN)'
				shp_new_row.setValue('DigBy',run_user)
				shp_new_row.setValue('DigDate',run_date)
				shp_new_row.setValue('DigOrg','NWIC')
				shp_new_row.setValue('Notes','automap APN:{0}'.format(apn_shape[1]))
				shp_to.insertRow(shp_new_row)			 				# stuff that sucker in there
				del shp_new_row
				count_shapes += 1
			del shp_to													# remove/close the InsertCursor
		else:
			count_noParcel += 1											# tally up APN's not found
			
				
	#----- end of loop: for reports in DocList
	arcpy.SetParameterAsText(1, output_shapefile_name)		# add to map
	arcpy.AddMessage("{0} Reports marked VOID".format(count_voids))
	arcpy.AddMessage("{0} Reports have no county specified".format(count_noCounty))
	arcpy.AddMessage("{0} Reports have no APN value".format(count_noAPN))
	arcpy.AddMessage("{0} Reports have mal-formed APN value".format(count_malformed))
	arcpy.AddMessage("{0} Reports with APN but no parcel found".format(count_noParcel))
	arcpy.AddMessage("{0} Reports with APN matching multiple parcels".format(count_multiParcel))
	arcpy.AddMessage("{0} parcel shapes copied".format(count_shapes))

def map_resources():
	arcpy.AddMessage("Mapping resources by APN")
	# loop through each Primary#
	
	# create the output feature
	out_template = r"MAIN\Resources\Resources (polygons)"
	(success, output_shapefile_name) = create_output_feature(DBsaved_selection_file,out_template)
	if not success:
		arcpy.AddError(output_shapefile_name)
		return
	
	# output_shapefile_name = make_output_file(DBsaved_selection_file,"parcelmapped","shp")
	# arcpy.AddMessage("output to: {0}".format(output_shapefile_name))
	# (out_path, out_name) = os.path.split(output_shapefile_name)
	# arcpy.CreateFeatureclass_management(out_path,out_name,"POLYGON",out_template,"SAME_AS_TEMPLATE","SAME_AS_TEMPLATE",out_template)
	shp_shape_field = arcpy.Describe(output_shapefile_name).ShapeFieldName				# get the fieldname of the field that holds the geometry
	
	count_voids = 0			# count primary numbers marked "Void"
	count_noAPN = 0			# count number of primarys with no APN in ICDB
	count_malformed = 0		# count records with potentially malformed APN's
	count_shapes = 0		# count number of parcel shapes output
	count_multiParcel = 0	# count number of primarys with >1 APN found in parcel layer
	count_noParcel = 0		# count number of primarys with 0 APN found in parcel layer
	
	for primary in ResList:
		(p_co,p_no) = (int(x) for x in primary.split('-'))
		current_primary = "P-{0:02}-{1:06}".format(p_co,p_no)
		arcpy.AddMessage("{0}:".format(current_primary))

		#----
		# start a connection and cursor to the ICDB
		icdb_connect = connect_to_icdb()
		icdb_cursor = icdb_connect.cursor(as_dict=True)

		#-----
		# get the parent table entry for this resource
		icdb_cursor.execute("Select * from tblResource WHERE PrimCo = {0} and PrimNo = {1}".format(p_co,p_no))
		icdb_resource_parent = icdb_cursor.next()	# we'll assume that there's only 1 row returned
		#-----
		# skip this if it's marked "VOIDED" in the ICDB
		if icdb_resource_parent['Voided']:
			arcpy.AddMessage("     {0} is marked VOIDED".format(current_primary))
			count_voids += 1
			continue
		#----
		# extract some field values 
		if icdb_resource_parent['ResourceName']:
			icdb_resource_name = icdb_resource_parent['ResourceName']
		else:
			icdb_resource_name = "[none]"
		
		#-----
		# find all the resource's address records
		icdb_cursor.execute("Select * from tblResourceAddr WHERE PrimCo = {0} and PrimNo = {1}".format(p_co,p_no))
		icdb_resource_apns = []
		for icdb_addr in icdb_cursor:
			if icdb_addr['APN']:
				apn_cleaned = icdb_addr['APN'].strip()	# remove leading/trailing whitespace
				if (len(apn_cleaned) > 0) and (apn_cleaned not in icdb_resource_apns):
					icdb_resource_apns.append(apn_cleaned)
		arcpy.AddMessage("     {0} APN's found in ICDB".format(len(icdb_resource_apns)))
		if len(icdb_resource_apns) == 0:
			count_noAPN += 1				# count primary with no APN value
			continue
		
		#------
		# loop through the APN's found for this Primary (i.e. icdb_resource_apns)
		# this gets a bit tricky... the parcel APN layers are messy.
		# So, while 1 Primary may be reasonably mapped to more than 1 APN
		# it is also possible that for a given APN, the parcel layer may have multiple shapes with that APN value
		parcel_layer = parcel_layers[p_co]			# get the layer name of the parcel
		if not parcel_selections_cleared[p_co]:
			arcpy.SelectLayerByAttribute_management(parcel_layer,"CLEAR_SELECTION")
			parcel_selections_cleared[p_co] = True
		found_malformed_apn = False					# clear this before looping through the APN's for this resource
		# go through the list of APN's found in the ICBD, for the current P-# and find them in the parcel layer
		apn_shapes = []								# store up the tuples with shape objects here
		for icdb_apn in icdb_resource_apns:
			if not re.match(apn_patterns[p_co],icdb_apn):
				arcpy.AddMessage("      APN '{0}' in {1} county may not be well-formed".format(icdb_apn,county_numbers[p_co]))
				found_malformed_apn = True			# found APN value that may not match in parcel layer
			cursor_apn = arcpy.da.SearchCursor(parcel_layer,["APN","SHAPE@"],"APN = '{0}'".format(icdb_apn))
			icdb_apn_count = 0
			for apn in cursor_apn:
				apn_shapes.append((icdb_apn,cursor_apn[1]))					# the SHAPE@ field is [1]
				icdb_apn_count += 1
			arcpy.AddMessage("     searching for APN {0} in {1} found {2} parcels".format(icdb_apn,parcel_layer,icdb_apn_count))
		
		# copy the parcel shapes to the output .shp file
		if len(apn_shapes) > 0:
			if len(apn_shapes) > 1:
				count_multiParcel += 1
			#-----
			# generate a feature in the output for each APN shape
			shp_to = arcpy.InsertCursor(output_shapefile_name)
			for apn_shape in apn_shapes:
				shp_new_row = shp_to.newRow()							# create empty record
				shp_new_row.setValue(shp_shape_field,apn_shape[1])
				shp_new_row.setValue('PrimCo',p_co)
				shp_new_row.setValue('PrimNo',p_no)
				if icdb_resource_parent['TrinNo'] > 0:					# if TrinNo is non-zero then copy it over, else leave Null
					shp_new_row.setValue('TrinNo',icdb_resource_parent['TrinNo'])
				shp_new_row.setValue('OtherID',icdb_resource_name)
				shp_new_row.setValue('DocSource','p')					# selects 'parcel (APN)'
				shp_new_row.setValue('DigSource','p')					# selects 'parcel (APN)'
				shp_new_row.setValue('DigBy',run_user)
				shp_new_row.setValue('DigDate',run_date)
				shp_new_row.setValue('DigOrg','NWIC')
				if icdb_resource_parent['TrinH']:
					shp_new_row.setValue('Notes','{0}; automap APN:{1}'.format(icdb_resource_parent['TrinH'],apn_shape[0]))
				else:
					shp_new_row.setValue('Notes','automap APN:{0}'.format(apn_shape[0]))
				shp_to.insertRow(shp_new_row)			 				# stuff that sucker in there
				del shp_new_row
				count_shapes += 1
			del shp_to		# remove/close the InsertCursor
		else:
			count_noParcel += 1

		if found_malformed_apn:
			count_malformed += 1
				
	#----- end of loop: for primary in ResList
	arcpy.SetParameterAsText(1, output_shapefile_name)		# add to map
	arcpy.AddMessage("{0} Primary #'s marked VOID".format(count_voids))
	arcpy.AddMessage("{0} Primary #'s have no APN value".format(count_noAPN))
	arcpy.AddMessage("{0} Primary #'s possibly mal-formed APN value".format(count_malformed))
	arcpy.AddMessage("{0} Primary with APN but no parcel found".format(count_noParcel))
	arcpy.AddMessage("{0} Primary #'s with APN matching multiple parcels".format(count_multiParcel))
	arcpy.AddMessage("{0} parcel shapes copied".format(count_shapes))

#================
# MAIN line code
#================

#-----------
# open the db saved selections file and see what it is
DBsave_wb = openpyxl.load_workbook(str(DBsaved_selection_file))
# advance the 'sheet' variable up to a recognized sheet name
for sheet in DBsave_wb.get_sheet_names():
	if sheet == sheet_saved_reports or sheet == sheet_saved_resources:
		break
# process, depending on which kind of saved selections were found
if sheet == sheet_saved_reports:
	tbl_Reports = DBsave_wb[sheet]
	if tbl_Reports['B1'].value != "DocNo" or tbl_Reports['A1'].value != "DocCo":
		arcpy.AddError("File is not a valid ICDB saved reports")
		sys.exit(1)
	DocList = []
	cellrow = 2		# start reading at row 2 of column 'B'
	while cellrow > 0:
		cellname = "B" + str(cellrow)
		if tbl_Reports[cellname].value is not None:
			DocList.append(tbl_Reports[cellname].value)		# add the S-# to the DocList
			cellrow += 1
		else:
			cellrow = 0
			break

	#----
	# process reports, S-#'s in DocList
	map_reports()

elif sheet == sheet_saved_resources:
	tbl_Resources = DBsave_wb[sheet]
	if tbl_Resources['B1'].value != "PrimNo" or tbl_Resources['A1'].value != "PrimCo":
		arcpy.AddError("File is not a valid ICDB saved resources")
		sys.exit(1)
	ResList = []
	cellrow = 2
	while cellrow > 0:
		cellCo = "A" + str(cellrow)
		cellNo = "B" + str(cellrow)
		if tbl_Resources[cellCo].value is not None and tbl_Resources[cellNo].value is not None:
			ResList.append("{0}-{1}".format(str(tbl_Resources[cellCo].value),str(tbl_Resources[cellNo].value)))
			cellrow += 1
		else:
			cellrow = 0
			break

	#----
	# process resources, P-#'s in ResList (formatted)
	#----
	arcpy.AddMessage("{0} Primary #'s input".format(len(ResList)))
	map_resources()

# we're here because the saved selections are for neither reports nor resources (prolly wrong file picked?)
else:
	arcpy.AddError("File is not a valid ICDB saved selections")
	sys.exit(1)
