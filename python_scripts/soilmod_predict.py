"""
Machine Learning model for 3D Cube Soil Generator using Gaussian Process Priors with mean functions.. 

Current models implemented:
- Gaussian Process with bayesian linear regression (BLR) as mean function and sparse spatial covariance function
- Gaussian Process with random forest (RF) regression as mean function and sparse spatial covariance function


Core functions:
- Training of mean function model and GP incl hyperparameter optimization
- generating soil property predictions and uncertainties

See documentation for more details.

User settings, such as input/output paths and all other options, are set in the settings file 
(Default filename: settings_soilmodel_predict.yaml) 
Alternatively, the settings file can be specified as a command line argument with: 
'-s', or '--settings' followed by PATH-TO-FILE/FILENAME.yaml 
(e.g. python featureimportance.py -s settings_featureimportance.yaml).

This package is part of the machine learning project developed for the Agricultural Research Federation (AgReFed).

Copyright 2022 Sebastian Haan, Sydney Informatics Hub (SIH), The University of Sydney

This open-source software is released under the AGPL-3.0 License.
"""

import numpy as np
import pandas as pd
import geopandas as gpd
import os
import sys
from scipy.special import erf
from scipy.interpolate import interp1d, griddata
import matplotlib.pyplot as plt
#import pyvista as pv # helper module for the Visualization Toolkit (VTK)
import subprocess
from sklearn.model_selection import train_test_split 
# Save and load trained models and scalers:
import pickle
import json
import yaml
from types import SimpleNamespace  
from tqdm import tqdm

# Custom local libraries:
from utils import array2geotiff, align_nearest_neighbor, print2, truncate_data
from sigmastats import averagestats
from preprocessing import gen_kfold
import GPmodel as gp # GP model plus kernel functions and distance matrix calculation

# Settings yaml file
_fname_settings = 'settings_soilmod_predict.yaml'

# flag to show plot figures interactively or not (True/False)
_show = False

# Load settings from yaml file
with open(_fname_settings, 'r') as f:
    settings = yaml.load(f, Loader=yaml.FullLoader)
# Parse settings dictinary as namespace (settings are available as 
# settings.variable_name rather than settings['variable_name'])
settings = SimpleNamespace(**settings)

#########################
if settings.calc_mean_only:
	settings.optimize_GP = False
	settings.calc_xval = False
	predict_grid_all = True


if settings.integrate_block:
    Nvoxel_per_block = settings.xblocksize * settings.yblocksize * settings.zblocksize / (settings.xvoxsize * settings.yvoxsize * settings.zvoxsize) # this is 125 in this case
    print("Number of evaluation points per block: ", Nvoxel_per_block)
    settings.xblocksize = settings.yblocksize = settings.xyblocksize

# currently assuming resolution is the same in x and y direction
settings.xvoxsize = settings.yvoxsize = settings.xyvoxsize


if type(settings.mean_functions) != list:
    settings.mean_functions = [settings.mean_functions]

for mean_function in settings.mean_functions:
    if mean_function == 'blr':
        import model_blr as blr
    if mean_function == 'rf':
        import model_rf as rf


######################### Main Script ############################
if __name__ == '__main__':	

    # check if outpath exists, if not create direcory
    os.makedirs(settings.outpath, exist_ok = True)

    # Intialise output info file:
    print2('init')
    print2(f'--- Parameter Settings ---')
    print2(f'Mean Functions: {settings.mean_functions}')
    print2(f'Target Name: {settings.name_target}')
    if settings.integrate_polygon:
        print2(f'Prediction geometry: Polygon')
    else:
        if settings.integrate_block:
            print2(f'Prediction geometry: Volume')
            print2(f'x,y,z blocksize: {settings.xyblocksize,settings.xyblocksize, settings.zblocksize}')

        else:
            print2(f'Prediction geometry: Point')
            print2(f'x,y,z voxsize: {settings.xyvoxsize,settings.xyvoxsize, settings.zvoxsize}')
    print2(f'--------------------------')

    print('Reading in data...')
    # Read in data for model training:
    dftrain = pd.read_csv(os.path.join(settings.inpath, settings.infname))

    # Select data between zmin and zmax
    dftrain = dftrain[(dftrain['z'] >= settings.zmin) & (dftrain['z'] <= settings.zmax)]

    name_features = settings.name_features



    #Read in grid covariates:

    """
    #check file extension
    _, file_extension = os.path.splitext(infname)
    if (file_extension == '.shp') | (file_extension == '.gpkg') | (file_extension == '.geojson'):
        import geopandas as gpd
        try:
            gdf = geopandas.read_file(shape)
        except DataIOError as err:
            # log error if you want
            # log(str(err))
            raise TypeError
    elif file_extension == '.csv':
        dfgrid, name_features_grid2 = preprocess_grid(inpath, gridname, name_features = name_features_grid)
    else:
        print('ERROR: Covariate grid file format not supported (need to be either .csv, .shp, .gpkg, or .geojson).')
    """


    #dfgrid, name_features_grid2 = preprocess_grid(inpath, gridname, name_features = name_features_grid, name_target = name_target, categorical = 'Soiltype')

    
    if integrate_polygon:
        import geopandas as gpd 
        dfgrid, dfpoly, name_features_grid2 = preprocess_grid_poly(settings.inpath, settings.gridname, settings.polyname, 
            name_features = settings.name_features_grid, name_target = settings.name_target, grid_crs = settings.project_crs, categorical = 'Soiltype')
    else:
        # read in covariate grid:
        dfgrid = pd.read_csv(os.path.join(settings.inpath, settings.gridname))


    ## Get coordinates for training data and set coord origin to (0,0)  
    bound_xmin = dfgrid.x.min() - 0.5* settings.xvoxsize
    bound_xmax = dfgrid.x.max() + 0.5* settings.xvoxsize
    bound_ymin = dfgrid.y.min() - 0.5* settings.yvoxsize
    bound_ymax = dfgrid.y.max() + 0.5* settings.yvoxsize

    dfgrid['x'] = dfgrid.x - bound_xmin
    dfgrid['y'] = dfgrid.y - bound_ymin

	# assert abs(dfgrid.x.unique() - xspace).sum() == 0
	# assert abs(dfgrid.y.unique() - yspace).sum() == 0
	
    # Define grid coordinates:
    points3D_train = np.asarray([dftrain.z.values, dftrain.y.values - bound_ymin, dftrain.x.values - bound_xmin ]).T

    # Define y target
    y_train = dftrain[name_target].values

    # spatial uncertainty of coordinates:
    Xdelta_train = np.asarray([0.5 * dftrain.z_diff.values, dftrain.y.values * 0, dftrain.x.values * 0.]).T

    # Calculate predicted mean values of training data
    X_train = dftrain[settings.name_features].values
    y_train = dftrain[settings.name_target].values
    if mean_function == 'rf':
        # Estimate GP mean function with Random Forest
        rf_model = rf.rf_train(X_train, y_train)
        ypred_rf_train, ynoise_train, nrmse_rf_train = rf.rf_predict(X_train, rf_model, y_test = y_train)
        y_train_fmean = ypred_rf_train
        # Plot mean function
        plt.figure()  # inches
        plt.title('Random Forest Model')
        plt.errorbar(y_train, ypred_rf_train, ynoise_train, linestyle='None', marker = 'o', c = 'r', label = 'Train Data')
        plt.errorbar(y_test, ypred_rf, ynoise_pred, linestyle='None', marker = 'o', c = 'b', label = 'Test Data')
        plt.legend(loc = 'upper left')
        plt.xlabel('y True')
        plt.ylabel('y Predict')
        plt.savefig(os.path.join(settings.outpath, settings.name_target + '_RF_pred_vs_true.png'), dpi = 300)
        if _show:
            plt.show()
        plt.close('all')
    elif mean_function == 'blr':
        # Scale data
        Xs_train, ys_train, scale_params = blr.scale_data(X_train, y_train)
        scaler_x, scaler_y = scale_params
        # Train BLR
        blr_model = blr.blr_train(Xs_train, y_train)
        # Predict for X_test
        ypred_blr_train, ypred_std_blr_train, nrmse_blr_train = blr.blr_predict(Xs_train, blr_model, y_test = y_train)
        ypred_blr_train = ypred_blr_train.flatten()
        y_train_fmean = ypred_blr_train
        ynoise_train = ypred_std_blr_train
        # Plot mean function
        plt.figure()  # inches
        plt.title('BLR Model')
        plt.scatter(y_train, ypred_blr_train, c = 'r', label='Train Data')
        plt.scatter(y_test, ypred_blr, c = 'b', label = 'Test Data')
        plt.legend(loc = 'upper left')
        plt.xlabel('y True')
        plt.ylabel('y Predict')
        plt.savefig(os.path.join(settings.outpath, settings.name_target + '_BLR_pred_vs_true.png'), dpi = 300)
        if _show:
            plt.show()
        plt.close('all')

    # Subtract mean function of depth from training data 
    y_train -= y_train_fmean

	# optimise GP hyperparameters 
	# Use mean of X uncertainity for optimizing since otherwise too many local minima
    if optimize_GP:
        print('Optimizing GP hyperparameters...')
        Xdelta_mean = Xdelta_train * 0 + np.nanmean(Xdelta_train,axis=0)
        opt_params, opt_logl = gp.optimize_gp_3D(points3D_train, y_train, ynoise_train, 
            xymin = settings.xyvoxsize, 
            zmin = settings.zvoxsize,  
            Xdelta = Xdelta_mean)
        params_gp = opt_params
    else:
        params_gp = GP_params


    extent = (0,bound_xmax - bound_xmin, 0, bound_ymax - bound_ymin)
    outpath_fig = os.path.join(settings.outpath, 'Figures_zslices/')
    os.makedirs(outpath_fig, exist_ok = True)	

    if settings.integrate_block:
        xblock = np.arange(dfgrid['x'].min(), dfgrid['x'].max(), settings.xblocksize) + 0.5 * settings.xblocksize
        yblock = np.arange(dfgrid['y'].min(), dfgrid['y'].max(), settings.yblocksize) + 0.5 * settings.yblocksize
        if (len(settings.list_z_pred) > 0) & (settings.list_z_pred is not None) &  (settings.list_z_pred != 'None'):
            zblock = np.asarray(settings.list_z_pred)
        else:
            zblock = np.arange(0.5 * settings.zblocksize, settings.zmax + 0.5 * settings.zblocksize, settings.zblocksize)
        block_x, block_y = np.meshgrid(xblock, yblock)
        block_shape = block_x.shape
        block_x = block_x.flatten()
        block_y = block_y.flatten()
        mu_3d = np.zeros((len(xblock), len(yblock), len(zblock)))
        std_3d = np.zeros((len(xblock), len(yblock), len(zblock)))
        mu_block = np.zeros_like(block_x)
        std_block = np.zeros_like(block_x)
        # Set initial optimisation of hyperparamter to True
        gp_train_flag = True
        # Slice in blocks for prediction calculating per 30 km x 1cm

        for i in range(len(zblock)):
            # predict for each depth z slice
            print('Computing slice at depth: ' + str(np.round(100 * zblock[i])) + 'cm')
            zrange = np.arange(zblock[i] - 0.5 * settings.zblocksize, zblock[i] + 0.5 * settings.zblocksize + settings.zvoxsize, settings.zvoxsize)
            ix_start = 0
            # Progressbar
            for j in tqdm(range(len(block_x.flatten()))):
                dftest = dfgrid[(dfgrid.x >= block_x[j] - 0.5 * settings.xblocksize) & (dfgrid.x <= block_x[j] + 0.5 * settings.xblocksize) &
                    (dfgrid.y >= block_y[j] - 0.5 * settings.yblocksize) & (dfgrid.y <= block_y[j] + 0.5 * settings.yblocksize)].copy()
                if len(dftest) > 0:
                    dfnew = dftest.copy()
                    for z in zrange:
                        if z == zrange[0]:
                            dftest['z'] = z 
                        else:
                            dfnew['z'] = z
                            dftest = dftest.append(dfnew, ignore_index = True)									
                    ysel = dftest.y.values
                    xsel = dftest.x.values
                    zsel = dftest.z.values
                    #zz, yy = np.meshgrid(zrange, ysel)
                    #zz, xx = np.meshgrid(zrange, xsel)
                    #points3D_pred = np.asarray([zz.flatten(), yy.flatten(), xx.flatten()]).T
                    points3D_pred = np.asarray([zsel, ysel, xsel]).T		
                    # Calculate mean function for prediction

                    if mean_function == 'rf':
                        X_test = dftest[settings.name_features].values
                        ypred_rf, ynoise_pred, _ = rf.rf_predict(X_test, rf_model)
                        y_pred_zmean = ypred_rf
                    elif mean_function == 'blr':
                        X_test = dftest[settings.name_features].values
                        Xs_test = scaler_x.transform(X_test)
                        ypred_blr, ypred_std_blr, _ = blr.blr_predict(Xs_test, blr_model)
                        ypred_blr = ypred_blr.flatten()
                        y_pred_zmean = ypred_blr
                        ynoise_pred = ypred_std_blr


                    # GP Prediction:
                    if not settings.calc_mean_only:
                        if gp_train_flag:
                            # Need to calculate matrix gp_train only once, then used subsequently for all other predictions
                            ypred, ystd, logl, gp_train, covar = gp.train_predict_3D(points3D_train, points3D_pred, y_train, ynoise_train, params_gp, 
                                Ynoise_pred = ynoise_pred, Xdelta = Xdelta_train, out_covar = True) 
                            gp_train_flag = False
                        else:
                            ypred, ystd, covar = gp.predict_3D(points3D_pred, gp_train, params_gp, Ynoise_pred = ynoise_pred, Xdelta = Xdelta_train, 
                                out_covar = True)
                    else:
                        ypred = y_pred_zmean
                        ystd = ynoise_pred

                    #### Need to calculate weighted average from covar and ypred
                    if not settings.calc_mean_only:
                        ypred_block, ystd_block = averagestats(ypred + y_pred_zmean, covar)
                    else:
                        ypred_block, ystd_block = averagestats(ypred, covar)


                    # Save results in block array
                    mu_block[j] = ypred_block
                    std_block[j] = ystd_block

                # Set blocks where there is no data to nan
                else:
                    mu_block[j] = np.nan
                    std_block[j] = np.nan


            # map coordinate array to image and save in 3D
            mu_img = mu_block.reshape(block_shape)
            std_img = std_block.reshape(block_shape)


            np.savetxt(os.path.join(outpath_fig, 'Pred_' + settings.name_target + '_z' + str("{:03d}".format(int(np.round(100 * zblock[i])))) + 'cm.txt'), np.round(mu_img.flatten(),3), delimiter=',')
            np.savetxt(os.path.join(outpath_fig, 'Pred_Stddev_' + settings.name_target + '_z' + str("{:03d}".format(int(np.round(100 * zblock[i])))) + 'cm.txt'), np.round(std_img.flatten(),3), delimiter=',')
            if i == 0:
                # Create coordinate array of x and y
                np.savetxt(os.path.join(outpath_fig, 'Pred_' + settings.name_target + '_coord_x.txt'), block_x, delimiter=',')
                np.savetxt(os.path.join(outpath_fig, 'Pred_' + settings.name_target + '_coord_y.txt'), block_y, delimiter=',')

            mu_3d[:,:,i] = mu_img.T
            std_3d[:,:,i] = std_img.T

            #mu_3d[~np.isnan(mu_3d) & (mu_3d > 100)] = np.nan

            #for i in range(3):
            # Create Result Plots
            mu_3d_trim = mu_3d[:,:,i].copy()
            mu_3d_trim_max = np.percentile(mu_3d_trim[~np.isnan(mu_3d_trim)], 99.5)
            mu_3d_trim[mu_3d_trim > mu_3d_trim_max] = mu_3d_trim_max
            mu_3d_trim[mu_3d_trim < 0] = 0
            plt.figure(figsize = (8,8))
            plt.subplot(2, 1, 1)
            plt.imshow(mu_3d_trim.T,origin='lower', aspect = 'equal', extent = extent, cmap = colormap_pred)
            plt.title(name_target + ' Depth ' + str(np.round(100 * zblock[i])) + 'cm')
            plt.ylabel('Northing [meters]')
            plt.colorbar()
            plt.subplot(2, 1, 2)
            std_3d_trim = std_3d[:,:,i].copy()
            std_3d_trim_max = np.percentile(std_3d_trim[~np.isnan(std_3d_trim)], 99.5)
            std_3d_trim[std_3d_trim > std_3d_trim_max] = std_3d_trim_max
            plt.imshow(std_3d_trim.T,origin='lower', aspect = 'equal', extent = extent, cmap = colormap_pred_std)
            plt.title('Std Dev ' + name_target + ' Depth ' + str(np.round(100 * zblock[i])) + 'cm')
            plt.colorbar()
            plt.xlabel('Easting [meters]')
            plt.ylabel('Northing [meters]')
            plt.tight_layout()
            plt.savefig(os.path.join(outpath_fig, 'Pred_' + settings.name_target + '_z' + str("{:03d}".format(int(np.round(100 * zblock[i])))) + 'cm.png'), dpi=300)
            if _show:
                plt.show()
            plt.close('all')
            

            #Save also as geotiff
            outfname_tif = os.path.join(outpath_fig, 'Pred_' + settings.name_target + '_z' + str("{:03d}".format(int(np.round(100 * zblock[i])))) + 'cm.tif')
            outfname_tif_std = os.path.join(outpath_fig, 'Std_' + settings.name_target + '_z' + str("{:03d}".format(int(np.round(100 * zblock[i])))) + 'cm.tif')
            #print('Saving results as geo tif...')
            tif_ok = array2geotiff(mu_img, [bound_xmin + 0.5 * settings.xblocksize,bound_ymin + 0.5 * settings.yblocksize], [settings.xblocksize,settings.yblocksize], outfname_tif, settings.project_crs)
            tif2_ok = array2geotiff(std_img, [bound_xmin + 0.5 * settings.xblocksize,bound_ymin + 0.5 * settings.yblocksize], [settings.xblocksize,settings.yblocksize], outfname_tif_std, settings.project_crs)


        # export cube data as vtk file, first change dimensions:
        esp_xyz = np.zeros((len(xblock), len(yblock), len(zblock)))
        std_xyz = np.zeros((len(xblock), len(yblock), len(zblock)))
        for i in range(len(zblock)):
            esp_xyz[:,:,i] = mu_3d[:,:,i].flatten().reshape(len(xblock), len(yblock))
            std_xyz[:,:,i] = std_3d[:,:,i].flatten().reshape(len(xblock), len(yblock))

        # Expand z dimension by factor 1000 for visualisation
        create_vtkcube(esp_xyz, origin=(0,0,0), voxelsize=(xblocksize,yblocksize,-zblocksize*1e3), fname= os.path.join(outpath_fig, name_target + '_depthx1000.vtk'))
        create_vtkcube(std_3d, origin=(0,0,0), voxelsize=(xblocksize,yblocksize,-zblocksize*1e3), fname= os.path.join(outpath_fig, 'Stddev_' + name_target + '_depthx1000.vtk'))

        # make constrain and probability maps
        for iconstrain in constrain_values_max:
            print('Creating probability maps ...')
            prob3d = create_probabilitymap(mu_3d, std_3d, zblock, zblock, iconstrain, outpath_fig)
        if len(zblock) > 1:
            print('Creating soil depth constrain maps ...')
            constrain_array, constrain_std_array = create_constrainmap_sigma(mu_3d, std_3d, zblock * 100, outpath_fig, values_min = constrain_values_min, values_max = constrain_values_max, interp = True)

	
    else:
		if settings.integrate_polygon:
			dfout_poly =  dfgrid[['ibatch']].copy()
		else:
			# Need to make predictions in mini-batches and then map results with coordinates to grid with ndimage.map_coordinates
			batchsize = 500
			#def chunker(df, batchsize):
			#	return (df[pos:pos + batchsize] for pos in np.arange(0, len(df), batchsize))
			dfgrid = dfgrid.reset_index()
			dfgrid['ibatch'] = dfgrid.index // batchsize
			
		#nbatch = dfgrid['ibatch'].max()
		ixrange_batch = dfgrid['ibatch'].unique()
		nbatch = len(ixrange_batch)
		print("Number of mini-batches per depth slice: ", nbatch)
		mu_res = np.zeros(len(dfgrid))
		std_res = np.zeros(len(dfgrid))
		coord_x = np.zeros(len(dfgrid))
		coord_y = np.zeros(len(dfgrid))
		ix = np.arange(len(dfgrid))

		xspace = np.arange(dfgrid['x'].min(), dfgrid['x'].max(), settings.xvoxsize)
		yspace = np.arange(dfgrid['y'].min(), dfgrid['y'].max(), settings.yvoxsize)
		if (len(list_z_pred) > 0) & (list_z_pred is not None) &  (list_z_pred != 'None'):
			zspace = np.asarray(list_z_pred)
		else:
			zspace = np.arange(settings.zvoxsize, settings.zmax + settings.zvoxsize, settings.zvoxsize)
			print('Calculating for depths at: ', zspace)
		grid_x, grid_y = np.meshgrid(xspace, yspace)
		mu_3d = np.zeros((len(xspace), len(yspace), len(zspace)))
		std_3d = np.zeros((len(xspace), len(yspace), len(zspace)))
		gp_train_flag = 0 # need to be computed only first time
		# Slice in blocks for prediction calculating per 30 km x 1cm
		for i in range(len(zspace)):
			# predict for each depth z slice
			print('Computing slices at depth: ' + str(np.round(100 * zspace[i])) + 'cm')
			ix_start = 0
			if settings.integrate_polygon:
				dfout_poly['Mean'] = np.nan
				dfout_poly['Std'] = np.nan
			for j in tqdm(ixrange_batch):
				dftest = dfgrid[dfgrid.ibatch == j].copy()
				#Set maximum number of evaluation points to 500 
				while len(dftest) > 500:
					# if larger than 500, select only subset of sample points that are regular spaced
					# select only every second value, this reduces size to 1/2
					dftest = dftest.sort_values(['y', 'x'], ascending = [True, True])
					dftest = dftest.iloc[::2, :]
				dftest['z'] = zspace[i]
				ysel = dftest.y.values
				xsel = dftest.x.values
				zsel = dftest.z.values
				points3D_pred = np.asarray([zsel, ysel, xsel]).T
				
				# Calculate mean function for prediction
				if mean_function == 'rf':
					X_test = dftest[settings.name_features].values
					ypred_rf, ynoise_pred, _ = rf.rf_predict(X_test, rf_model)
					y_pred_zmean = ypred_rf
				elif mean_function == 'blr':
					X_test = dftest[settings.name_features].values
					Xs_test = scaler_x.transform(X_test)
					ypred_blr, ypred_std_blr, _ = blr.blr_predict(Xs_test, blr_model)
					y_pred_zmean = ypred_blr
					ynoise_pred = ypred_std_blr

				# GP Prediction:
				if not settings.calc_mean_only:
					if gp_train_flag == 0:
						# Need to calculate matrix gp_train only once, then used subsequently for all other predictions
						if settings.integrate_polygon:
							ypred, ystd, logl, gp_train, covar = gp.train_predict_3D(points3D_train, points3D_pred, y_train, ynoise_train, params_gp, 
								Ynoise_pred = ynoise_pred, Xdelta = Xdelta_train, out_covar = True)
						else:
							ypred, ystd, logl, gp_train = gp.train_predict_3D(points3D_train, points3D_pred, y_train, ynoise_train, params_gp, 
								Ynoise_pred = ynoise_pred, Xdelta = Xdelta_train)
						gp_train_flag = 1
					else:
						if settings.integrate_polygon:
							ypred, ystd, covar = gp.predict_3D(points3D_pred, gp_train, params_gp, Ynoise_pred = ynoise_pred, 
								Xdelta = Xdelta_train, out_covar = True)
						else:
							ypred, ystd = gp.predict_3D(points3D_pred, gp_train, params_gp, Ynoise_pred = ynoise_pred, Xdelta = Xdelta_train)
				else:
					ypred = y_pred_zmean
					ystd = ynoise_pred

				# Combine noise of GP and mean functiojn for prediction (already in coavraice function):
				#ystd = np.sqrt(ystd**2 + ynoise_pred**2)	

				if settings.integrate_polygon:
					# Now calculate mean and standard deviation for polygon area
					# Need to calculate weighted average from covar and ypred
					if not settings.calc_mean_only:
						ypred_poly, ystd_poly = averagestats(ypred + y_pred_zmean, covar)
					else:
						ypred_poly, ystd_poly = averagestats(ypred, covar)
					dfout_poly.loc[dfout_poly['ibatch'] == j, 'Mean'] = ypred_poly
					dfout_poly.loc[dfout_poly['ibatch'] == j, 'Std'] = ystd_poly
				else:
					# Save results in 3D array
					ix_end = ix_start + len(ypred)
					if not settings.calc_mean_only:
						mu_res[ix_start : ix_end] = ypred + y_pred_zmean #.reshape(len(xspace), len(yspace))
						std_res[ix_start : ix_end] = ystd #.reshape(len(xspace), len(yspace))
					else: 
						mu_res[ix_start : ix_end] = ypred #.reshape(len(xspace), len(yspace))
						std_res[ix_start : ix_end] = ystd #.reshape(len(xspace), len(yspace))
					if i ==0:
						coord_x[ix_start : ix_end] = np.round(dftest.x.values,2)
						coord_y[ix_start : ix_end] = np.round(dftest.y.values,2)
					ix_start = ix_end


			# Save all data for the depth layer
			
			if settings.integrate_polygon:
                dfpoly_z = dfpoly.merge(dfout_poly, how = 'left', on = 'ibatch')
                # Save results with polygon shape as Geopackage (can e.g. visualised in QGIS)
                dfpoly_z.to_file(os.path.join(outpath_fig, 'Prediction_poly_' + settings.name_target + '_z' + str("{:03d}".format(int(np.round(100 * zspace[i])))) + 'cm.gpkg'), driver='GPKG')
                # make some plots with geopandas
                print("Plotting polygon map ...")
                fig, (ax1, ax2) = plt.subplots(ncols = 1, nrows=2, sharex=True, sharey=True, figsize = (10,10))
                dfpoly_z.plot(column='Mean', legend=True, ax = ax1, cmap = colormap_pred)#, legend_kwds={'label': "",'orientation': "vertical"}))
                ax1.title.set_text('Mean ' + settings.name_target + ' Depth ' + str(np.round(100 * zspace[i])) + 'cm')
                ax1.set_ylabel('Northing [meters]')
                #plt.xlabel('Easting [meters]')
                #plt.savefig(os.path.join(outpath_fig, 'Pred_Mean_Poly_' + name_target + '_z' + str("{:03d}".format(int(np.round(100 * zspace[i])))) + 'cm.png'), dpi=300)
                dfpoly_z.plot(column='Std', legend=True,  ax = ax2, cmap = colormap_pred_std)#, legend_kwds={'label': "",'orientation': "vertical"}))
                ax2.title.set_text('Std Dev ' + settings.name_target + ' Depth ' + str(np.round(100 * zspace[i])) + 'cm')
                ax2.set_xlabel('Easting [meters]')
                ax2.set_ylabel('Northing [meters]')
                plt.tight_layout()
                plt.savefig(os.path.join(outpath_fig, 'Pred_Poly_' + settings.name_target + '_z' + str("{:03d}".format(int(np.round(100 * zspace[i])))) + 'cm.png'), dpi=300)
                if _show:
                    plt.show()  
                plt.close('all')
			else:
                print("saving data and generating plots...")
                # map coordinate array to image
                #mu_img = griddata(np.asarray([coord_x, coord_y]).T, mu_res, (grid_x, grid_y), method = 'nearest')
                #std_img = griddata(np.asarray([coord_x, coord_y]).T, std_res, (grid_x, grid_y), method = 'nearest')

                #mask_img = np.zeros_like(grid_x.flatten()) * np.nan
                mu_img = np.zeros_like(grid_x.flatten()) * np.nan
                std_img = np.zeros_like(grid_x.flatten()) * np.nan
                xgridflat = grid_x.flatten()
                ygridflat = grid_y.flatten()


                # Calculate nearest neighbor
                xygridflat = np.asarray([xgridflat, ygridflat]).T
                coord_xy = np.asarray([coord_x, coord_y]).T
                mu_img, std_img = align_nearest_neighbor(xygridflat, coord_xy, [mu_res, std_res], max_dist = 0.5 * settings.xvoxsize)

                np.savetxt(os.path.join(outpath_fig, 'Pred_' + settings.name_target + '_z' + str("{:03d}".format(int(np.round(100 * zspace[i])))) + 'cm.txt'), np.round(mu_img,2), delimiter=',')
                np.savetxt(os.path.join(outpath_fig, 'Pred_Stddev_' + settings.name_target + '_z' + str("{:03d}".format(int(np.round(100 * zspace[i])))) + 'cm.txt'), np.round(std_img,3), delimiter=',')
                if i == 0:
                    # Create coordinate array of x and y
                    np.savetxt(os.path.join(outpath_fig, 'Pred_' + settings.name_target + '_coord_x.txt'), coord_x, delimiter=',')
                    np.savetxt(os.path.join(outpath_fig, 'Pred_' + settings.name_target + '_coord_y.txt'), coord_y, delimiter=',')
                
                mu_img = mu_img.reshape(grid_x.shape)
                std_img = std_img.reshape(grid_x.shape)

                mu_3d[:,:,i] = mu_img.T
                std_3d[:,:,i] = std_img.T

                #mu_3d[~np.isnan(mu_3d) & (mu_3d > 100)] = np.nan

                #for i in range(3):
                # Create Result Plots
                print("Creating plots...")
                mu_3d_trim = mu_3d[:,:,i].copy()
                mu_3d_trim_max = np.percentile(mu_3d_trim[~np.isnan(mu_3d_trim)], 99.5)
                mu_3d_trim[mu_3d_trim > mu_3d_trim_max] = mu_3d_trim_max
                mu_3d_trim[mu_3d_trim < 0] = 0
                plt.figure(figsize = (8,8))
                plt.subplot(2, 1, 1)
                plt.imshow(mu_3d_trim.T,origin='lower', aspect = 'equal', extent = extent, cmap = colormap_pred)
                #plt.imshow(ystd.reshape(len(yspace),len(xspace)),origin='lower', aspect = 'equal', extent = extent) 
                #plt.scatter(points3D_train[:,2],points3D_train[:,1], edgecolors = 'k',facecolors='none')
                plt.title(settings.name_target + ' Depth ' + str(np.round(100 * zspace[i])) + 'cm')
                plt.ylabel('Northing [meters]')
                plt.colorbar()
                plt.subplot(2, 1, 2)
                std_3d_trim = std_3d[:,:,i].copy()
                std_3d_trim_max = np.percentile(std_3d_trim[~np.isnan(std_3d_trim)], 99.5)
                std_3d_trim[std_3d_trim > std_3d_trim_max] = std_3d_trim_max
                std_3d_trim[std_3d_trim < 0] = 0
                plt.imshow(std_3d_trim.T,origin='lower', aspect = 'equal', extent = extent, cmap = colormap_pred_std)
                #plt.imshow(ystd.reshape(len(yspace),len(xspace)),origin='lower', aspect = 'equal', extent = extent) 
                #plt.scatter(points3D_train[:,2],points3D_train[:,1], edgecolors = 'k',facecolors='none')
                plt.title('Std Dev ' + settings.name_target + ' Depth ' + str(np.round(100 * zspace[i])) + 'cm')
                plt.colorbar()
                plt.xlabel('Easting [meters]')
                plt.ylabel('Northing [meters]')
                plt.tight_layout()
                plt.savefig(os.path.join(outpath_fig, 'Pred_' + settings.name_target + '_z' + str("{:03d}".format(int(np.round(100 * zspace[i])))) + 'cm.png'), dpi=300)
                if _show:
                    plt.show()
                plt.close('all')

                #Save also as geotiff
                outfname_tif = os.path.join(outpath_fig, 'Pred_' + settings.name_target + '_z' + str("{:03d}".format(int(np.round(100 * zspace[i])))) + 'cm.tif')
                outfname_tif_std = os.path.join(outpath_fig, 'Std_' + settings.name_target + '_z' + str("{:03d}".format(int(np.round(100 * zspace[i])))) + 'cm.tif')

                print('Saving results as geo tif...')
                tif_ok = array2geotiff(mu_img, [bound_xmin + 0.5 * xvoxsize, bound_ymin + 0.5 * yvoxsize], [xvoxsize,yvoxsize], outfname_tif, project_crs)
                tif2_ok = array2geotiff(std_img, [bound_xmin + 0.5 * xvoxsize, bound_ymin + 0.5 * yvoxsize], [xvoxsize,yvoxsize], outfname_tif_std, project_crs)


		print("Exporting cube...")
		# export cube data as vtk file, first change dimensions:
		if not settings.integrate_polygon:
			esp_xyz = np.zeros((len(xspace), len(yspace), len(zspace)))
			std_xyz = np.zeros((len(xspace), len(yspace), len(zspace)))
			for i in range(len(zspace)):
				esp_xyz[:,:,i] = mu_3d[:,:,i].flatten().reshape(len(xspace), len(yspace))
				std_xyz[:,:,i] = std_3d[:,:,i].flatten().reshape(len(xspace), len(yspace))

			# Expand z dimension by factor 1000 for visualisation
			create_vtkcube(esp_xyz, origin=(0,0,0), voxelsize=(settings.xvoxsize,settings.yvoxsize,-settings.zvoxsize*1e3), fname= os.path.join(outpath_fig, settings.name_target + '_depthx1000.vtk'))
			create_vtkcube(std_3d, origin=(0,0,0), voxelsize=(settings.xvoxsize,settings.yvoxsize,-settings.zvoxsize*1e3), fname= os.path.join(outpath_fig, 'Stddev_' + settings.name_target + '_depthx1000.vtk'))

		if not settings.integrate_polygon:
			# make constrain and probability maps
			try:
				print('Creating probability maps ...')
				for iconstrain in constrain_values_max:
					prob3d = create_probabilitymap(mu_3d, std_3d, zspace, zspace, iconstrain, outpath_fig)
			except:
				print('Probablity Map creation failed')
			if len(zspace) > 1:
				print('Creating soil depth constrain maps ...')
				constrain_array, constrain_std_array = create_constrainmap_sigma(mu_3d, std_3d, zspace * 100, outpath_fig, values_min = constrain_values_min, values_max = constrain_values_max, interp = True)

			

    if not settings.integrate_polygon:
        # Clip stddev for images
        mu_3d_mean = mu_3d.mean(axis = 2).T
        mu_3d_mean_max = np.percentile(mu_3d_mean,99.5)
        mu_3d_mean_trim = mu_3d_mean.copy()
        mu_3d_mean_trim[mu_3d_mean > mu_3d_mean_max] = mu_3d_mean_max
        mu_3d_mean_trim[mu_3d_mean < 0] = 0
        std_3d_trim = std_3d.copy()
        std_3d_trim_max = np.percentile(std_3d_trim[~np.isnan(std_3d_trim)],99.5)
        std_3d_trim[std_3d_trim > std_3d_trim_max] = std_3d_trim_max
        std_3d_trim[std_3d_trim < 0] = 0

        # Create Result Plot of mean with locations
        plt.figure(figsize = (8,8))
        plt.subplot(2, 1, 1)
        plt.imshow(mu_3d_mean_trim,origin='lower', aspect = 'equal', extent = extent, cmap = colormap_pred)
        plt.colorbar()
        #plt.imshow(ystd.reshape(len(yspace),len(xspace)),origin='lower', aspect = 'equal', extent = extent) 
        plt.scatter(points3D_train[:,2],points3D_train[:,1], edgecolors = 'k',facecolors='none')
        plt.title('ESP ' +settings. name_target + ' Mean')
        plt.ylabel('Northing [meters]')
        
        plt.subplot(2, 1, 2)
        plt.imshow(std_3d_trim.mean(axis = 2).T,origin='lower', aspect = 'equal', extent = extent, cmap = colormap_pred_std)
        plt.colorbar()
        #plt.imshow(ystd.reshape(len(yspace),len(xspace)),origin='lower', aspect = 'equal', extent = extent) 
        plt.scatter(points3D_train[:,2],points3D_train[:,1], edgecolors = 'k',facecolors='none')
        plt.title('Std Dev ' + settings.name_target + ' Mean')
        plt.xlabel('Easting [meters]')
        plt.ylabel('Northing [meters]')
        plt.tight_layout()
        plt.savefig(os.path.join(outpath_fig, 'Pred_' + name_target + '_mean.png'), dpi=300)
        if _show:
            plt.show()
        plt.close('all')

        # Create Result Plot with data colors
        plt.figure(figsize = (8,8))
        plt.subplot(2, 1, 1)
        plt.imshow(mu_3d_mean_trim,origin='lower', aspect = 'equal', extent = extent, cmap = colormap_pred)
        plt.colorbar()
        #plt.imshow(ystd.reshape(len(yspace),len(xspace)),origin='lower', aspect = 'equal', extent = extent) 
        plt.scatter(points3D_train[:,2],points3D_train[:,1], c = dftrain[name_target].values, alpha =0.3, edgecolors = 'k')
        
        plt.title(settings.name_target + ' Depth Mean')
        plt.ylabel('Northing [meters]')
        plt.subplot(2, 1, 2)
        plt.imshow(std_3d_trim.mean(axis = 2).T,origin='lower', aspect = 'equal', extent = extent, cmap = colormap_pred_std)
        #plt.imshow(np.sqrt((std_3d.mean(axis = 2).T)**2 + params_gp[1]**2 *  ytrain.std()**2),origin='lower', aspect = 'equal', extent = extent)
        plt.colorbar()
        #plt.imshow(ystd.reshape(len(yspace),len(xspace)),origin='lower', aspect = 'equal', extent = extent) 
        plt.scatter(points3D_train[:,2],points3D_train[:,1], edgecolors = 'k',facecolors='none')
        plt.title('Std Dev ' + settings.name_target + ' Mean')
        plt.xlabel('Easting [meters]')
        plt.ylabel('Northing [meters]')
        plt.tight_layout()
        plt.savefig(os.path.join(outpath_fig, 'Pred_' + name_target + '_mean2.png'), dpi=300)
        if _show:
            plt.show()
        plt.close('all')


    print("Prediction Mean, Median, Std, 25Perc, 75Perc:", np.round([np.nanmean(mu_3d), np.median(mu_3d[~np.isnan(mu_3d)]), 
        np.nanstd(mu_3d), np.percentile(mu_3d[~np.isnan(mu_3d)],25), np.percentile(mu_3d[~np.isnan(mu_3d)],75)] 
        ,3))
    print("Uncertainty Mean, Median, Std, 25Perc, 75Perc:", np.round([np.nanmean(std_3d), np.median(std_3d[~np.isnan(std_3d)]),
        np.nanstd(std_3d), np.percentile(std_3d[~np.isnan(std_3d)],25), np.percentile(std_3d[~np.isnan(std_3d)],75)],3))


