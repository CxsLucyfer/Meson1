# Copyright 2014 The Meson development team

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at

#     http://www.apache.org/licenses/LICENSE-2.0

# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os, sys
import backends, build
import xml.etree.ElementTree as ET
import xml.dom.minidom
from coredata import MesonException

class Vs2010Backend(backends.Backend):
    def __init__(self, build, interp):
        super().__init__(build, interp)
        self.project_file_version = '10.0.30319.1'
        # foo.c compiles to foo.obj, not foo.c.obj
        self.source_suffix_in_obj = False

    def generate_custom_generator_commands(self, target, parent_node):
        idgroup = ET.SubElement(parent_node, 'ItemDefinitionGroup')
        all_output_files = []
        for genlist in target.get_generated_sources():
            generator = genlist.get_generator()
            exe = generator.get_exe()
            infilelist = genlist.get_infilelist()
            outfilelist = genlist.get_outfilelist()
            if isinstance(exe, build.BuildTarget):
                exe_file = os.path.join(self.environment.get_build_dir(), self.get_target_filename(exe))
            else:
                exe_file = exe.get_command()
            base_args = generator.get_arglist()
            for i in range(len(infilelist)):
                if len(infilelist) == len(outfilelist):
                    sole_output = os.path.join(self.get_target_private_dir(target), outfilelist[i])
                else:
                    sole_output = ''
                curfile = infilelist[i]
                infilename = os.path.join(self.environment.get_source_dir(), curfile)
                outfiles = genlist.get_outputs_for(curfile)
                outfiles = [os.path.join(self.get_target_private_dir(target), of) for of in outfiles]
                all_output_files += outfiles
                args = [x.replace("@INPUT@", infilename).replace('@OUTPUT@', sole_output)\
                        for x in base_args]
                args = [x.replace("@SOURCE_DIR@", self.environment.get_source_dir()).replace("@BUILD_DIR@", self.get_target_private_dir(target))
                        for x in args]
                fullcmd = [exe_file] + args
                cbs = ET.SubElement(idgroup, 'CustomBuildStep')
                ET.SubElement(cbs, 'Command').text = ' '.join(self.special_quote(fullcmd))
                ET.SubElement(cbs, 'Inputs').text = infilename
                ET.SubElement(cbs, 'Outputs').text = ';'.join(outfiles)
                ET.SubElement(cbs, 'Message').text = 'Generating sources from %s.' % infilename
        pg = ET.SubElement(parent_node, 'PropertyGroup')
        ET.SubElement(pg, 'CustomBuildBeforeTargets').text = 'ClCompile'
        return all_output_files

    def generate(self):
        self.generate_pkgconfig_files()
        sln_filename = os.path.join(self.environment.get_build_dir(), self.build.project_name + '.sln')
        projlist = self.generate_projects()
        self.gen_testproj('RUN_TESTS', os.path.join(self.environment.get_build_dir(), 'RUN_TESTS.vcxproj'))
        self.generate_solution(sln_filename, projlist)

    def get_obj_target_deps(self, obj_list):
        result = {}
        for o in obj_list:
            if isinstance(o, build.ExtractedObjects):
                result[o.target.get_basename()] = True
        return result.keys()

    def generate_solution(self, sln_filename, projlist):
        ofile = open(sln_filename, 'w')
        ofile.write('Microsoft Visual Studio Solution File, Format Version 11.00\n')
        ofile.write('# Visual Studio 2010\n')
        prj_templ = prj_line = 'Project("{%s}") = "%s", "%s", "{%s}"\n'
        for p in projlist:
            prj_line = prj_templ % (self.environment.coredata.guid, p[0], p[1], p[2])
            ofile.write(prj_line)
            all_deps = {}
            for ldep in self.build.targets[p[0]].link_targets:
                all_deps[ldep.get_basename()] = True
            for objdep in self.get_obj_target_deps(self.build.targets[p[0]].objects):
                all_deps[objdep] = True
            for gendep in self.build.targets[p[0]].generated:
                gen_exe = gendep.generator.get_exe()
                if isinstance(gen_exe, build.Executable):
                    all_deps[gen_exe.get_basename()] = True
            if len(all_deps) > 0:
                ofile.write('\tProjectSection(ProjectDependencies) = postProject\n')
                for dep in all_deps.keys():
                    guid = self.environment.coredata.target_guids[dep]
                    ofile.write('\t\t{%s} = {%s}\n' % (guid, guid))
                ofile.write('EndProjectSection\n')
            ofile.write('EndProject\n')
        test_line = prj_templ % (self.environment.coredata.guid,
                                 'RUN_TESTS', 'RUN_TESTS.vcxproj', self.environment.coredata.test_guid)
        ofile.write(test_line)
        ofile.write('EndProject\n')
        ofile.write('Global\n')
        ofile.write('\tGlobalSection(SolutionConfigurationPlatforms) = preSolution\n')
        ofile.write('\t\tDebug|Win32 = Debug|Win32\n')
        ofile.write('\tEndGlobalSection\n')
        ofile.write('\tGlobalSection(ProjectConfigurationPlatforms) = postSolution\n')
        for p in projlist:
            ofile.write('\t\t{%s}.Debug|Win32.ActiveCfg = Debug|Win32\n' % p[2])
            ofile.write('\t\t{%s}.Debug|Win32.Build.0 = Debug|Win32\n' % p[2])
        ofile.write('\t\t{%s}.Debug|Win32.ActiveCfg = Debug|Win32\n' % self.environment.coredata.test_guid)
        ofile.write('\tEndGlobalSection\n')
        ofile.write('\tGlobalSection(SolutionProperties) = preSolution\n')
        ofile.write('\t\tHideSolutionNode = FALSE\n')
        ofile.write('\tEndGlobalSection\n')
        ofile.write('EndGlobal\n')

    def generate_projects(self):
        projlist = []
        for name, target in self.build.targets.items():
            outdir = os.path.join(self.environment.get_build_dir(), target.subdir)
            fname = name + '.vcxproj'
            relname = os.path.join(target.subdir, fname)
            projfile = os.path.join(outdir, fname)
            uuid = self.environment.coredata.target_guids[name]
            self.gen_vcxproj(target, projfile, uuid)
            projlist.append((name, relname, uuid))
        return projlist

    def split_sources(self, srclist):
        sources = []
        headers = []
        for i in srclist:
            if self.environment.is_header(i):
                headers.append(i)
            else:
                sources.append(i)
        return (sources, headers)

    def target_to_build_root(self, target):
        if target.subdir == '':
            return ''

        directories = os.path.split(target.subdir)
        directories = list(filter(bool,directories)) #Filter out empty strings

        return '/'.join(['..']*len(directories))

    def special_quote(self, arr):
        return ['&quot;%s&quot;' % i for i in arr]

    def gen_vcxproj(self, target, ofname, guid):
        down = self.target_to_build_root(target)
        proj_to_src_root = os.path.join(down, self.build_to_src)
        proj_to_src_dir = os.path.join(proj_to_src_root, target.subdir)
        (sources, headers) = self.split_sources(target.sources)
        entrypoint = 'WinMainCRTStartup'
        buildtype = 'Debug'
        platform = "Win32"
        project_name = target.name
        target_name = target.name
        subsystem = 'Windows'
        if isinstance(target, build.Executable):
            conftype = 'Application'
            if not target.gui_app:
                subsystem = 'Console'
                entrypoint = 'mainCRTStartup'
        elif isinstance(target, build.StaticLibrary):
            conftype = 'StaticLibrary'
        elif isinstance(target, build.SharedLibrary):
            conftype = 'DynamicLibrary'
            entrypoint = '_DllMainCrtStartup'
        else:
            raise MesonException('Unknown target type for %s' % target_name)
        root = ET.Element('Project', {'DefaultTargets' : "Build",
                                      'ToolsVersion' : '4.0',
                                      'xmlns' : 'http://schemas.microsoft.com/developer/msbuild/2003'})
        confitems = ET.SubElement(root, 'ItemGroup', {'Label' : 'ProjectConfigurations'})
        prjconf = ET.SubElement(confitems, 'ProjectConfiguration', {'Include' : 'Debug|Win32'})
        p = ET.SubElement(prjconf, 'Configuration')
        p.text= buildtype
        pl = ET.SubElement(prjconf, 'Platform')
        pl.text = platform
        globalgroup = ET.SubElement(root, 'PropertyGroup', Label='Globals')
        guidelem = ET.SubElement(globalgroup, 'ProjectGuid')
        guidelem.text = guid
        kw = ET.SubElement(globalgroup, 'Keyword')
        kw.text = 'Win32Proj'
        ns = ET.SubElement(globalgroup, 'RootNamespace')
        ns.text = target_name
        p = ET.SubElement(globalgroup, 'Platform')
        p.text= platform
        pname= ET.SubElement(globalgroup, 'ProjectName')
        pname.text = project_name
        ET.SubElement(root, 'Import', Project='$(VCTargetsPath)\Microsoft.Cpp.Default.props')
        type_config = ET.SubElement(root, 'PropertyGroup', Label='Configuration')
        ET.SubElement(type_config, 'ConfigurationType').text = conftype
        ET.SubElement(type_config, 'CharacterSet').text = 'MultiByte'
        ET.SubElement(type_config, 'WholeProgramOptimization').text = 'false'
        ET.SubElement(type_config, 'UseDebugLibraries').text = 'true'
        ET.SubElement(root, 'Import', Project='$(VCTargetsPath)\Microsoft.Cpp.props')
        generated_files = self.generate_custom_generator_commands(target, root)
        (gen_src, gen_hdrs) = self.split_sources(generated_files)
        direlem = ET.SubElement(root, 'PropertyGroup')
        fver = ET.SubElement(direlem, '_ProjectFileVersion')
        fver.text = self.project_file_version
        outdir = ET.SubElement(direlem, 'OutDir')
        outdir.text = '.\\'
        intdir = ET.SubElement(direlem, 'IntDir')
        intdir.text = os.path.join(self.get_target_dir(target), target.get_basename() + '.dir') + '\\'
        tname = ET.SubElement(direlem, 'TargetName')
        tname.text = target_name
        inclinc = ET.SubElement(direlem, 'LinkIncremental')
        inclinc.text = 'true'

        compiles = ET.SubElement(root, 'ItemDefinitionGroup')
        clconf = ET.SubElement(compiles, 'ClCompile')
        opt = ET.SubElement(clconf, 'Optimization')
        opt.text = 'disabled'
        inc_dirs = [proj_to_src_dir, self.get_target_private_dir(target)]
        cur_dir = target.subdir
        if cur_dir == '':
            cur_dir= '.'
        inc_dirs.append(cur_dir)
        extra_args = []
        # SUCKS, VS can not handle per-language type flags, so just use
        # them all.
        for l in self.build.global_args.values():
            for a in l:
                extra_args.append(a)
        for l in target.extra_args.values():
            for a in l:
                extra_args.append(a)
        if len(extra_args) > 0:
            extra_args.append('%(AdditionalOptions)')
            ET.SubElement(clconf, "AdditionalOptions").text = ' '.join(extra_args)
        for d in target.include_dirs:
            for i in d.incdirs:
                curdir = os.path.join(d.curdir, i)
                inc_dirs.append(self.relpath(curdir, target.subdir)) # build dir
                inc_dirs.append(os.path.join(proj_to_src_root, curdir)) # src dir
        inc_dirs.append('%(AdditionalIncludeDirectories)')
        ET.SubElement(clconf, 'AdditionalIncludeDirectories').text = ';'.join(inc_dirs)
        preproc = ET.SubElement(clconf, 'PreprocessorDefinitions')
        rebuild = ET.SubElement(clconf, 'MinimalRebuild')
        rebuild.text = 'true'
        rtlib = ET.SubElement(clconf, 'RuntimeLibrary')
        rtlib.text = 'MultiThreadedDebugDLL'
        funclink = ET.SubElement(clconf, 'FunctionLevelLinking')
        funclink.text = 'true'
        pch = ET.SubElement(clconf, 'PrecompiledHeader')
        warnings = ET.SubElement(clconf, 'WarningLevel')
        warnings.text = 'Level3'
        debinfo = ET.SubElement(clconf, 'DebugInformationFormat')
        debinfo.text = 'EditAndContinue'
        resourcecompile = ET.SubElement(compiles, 'ResourceCompile')
        ET.SubElement(resourcecompile, 'PreprocessorDefinitions')
        link = ET.SubElement(compiles, 'Link')
        additional_links = []
        for t in target.link_targets:
            lobj = self.build.targets[t.get_basename()]
            rel_path = self.relpath(lobj.subdir, target.subdir)
            linkname = os.path.join(rel_path, lobj.get_import_filename())
            additional_links.append(linkname)
        for o in self.flatten_object_list(target, down):
            assert(isinstance(o, str))
            additional_links.append(o)
        if len(additional_links) > 0:
            additional_links.append('%(AdditionalDependencies)')
            ET.SubElement(link, 'AdditionalDependencies').text = ';'.join(additional_links)
        ofile = ET.SubElement(link, 'OutputFile')
        ofile.text = '$(OutDir)%s' % target.get_filename()
        addlibdir = ET.SubElement(link, 'AdditionalLibraryDirectories')
        addlibdir.text = '%(AdditionalLibraryDirectories)'
        subsys = ET.SubElement(link, 'SubSystem')
        subsys.text = subsystem
        gendeb = ET.SubElement(link, 'GenerateDebugInformation')
        gendeb.text = 'true'
        if isinstance(target, build.SharedLibrary):
            ET.SubElement(link, 'ImportLibrary').text = target.get_import_filename()
        pdb = ET.SubElement(link, 'ProgramDataBaseFileName')
        pdb.text = '$(OutDir}%s.pdb' % target_name
        if isinstance(target, build.Executable):
            ET.SubElement(link, 'EntryPointSymbol').text = entrypoint
        targetmachine = ET.SubElement(link, 'TargetMachine')
        targetmachine.text = 'MachineX86'

        if len(headers) + len(gen_hdrs) > 0:
            inc_hdrs = ET.SubElement(root, 'ItemGroup')
            for h in headers:
                relpath = h.rel_to_builddir(proj_to_src_root)
                ET.SubElement(inc_hdrs, 'CLInclude', Include=relpath)
            for h in gen_hdrs:
                relpath = h.rel_to_builddir(proj_to_src_root)
                ET.SubElement(inc_hdrs, 'CLInclude', Include = relpath)
        if len(sources) + len(gen_src) > 0:
            inc_src = ET.SubElement(root, 'ItemGroup')
            for s in sources:
                relpath = s.rel_to_builddir(proj_to_src_root)
                ET.SubElement(inc_src, 'CLCompile', Include=relpath)
            for s in gen_src:
                relpath =  self.relpath(s, target.subdir)
                ET.SubElement(inc_src, 'CLCompile', Include=relpath)
        ET.SubElement(root, 'Import', Project='$(VCTargetsPath)\Microsoft.Cpp.targets')
        tree = ET.ElementTree(root)
        tree.write(ofname, encoding='utf-8', xml_declaration=True)
        # ElementTree can not do prettyprinting so do it manually
        doc = xml.dom.minidom.parse(ofname)
        open(ofname, 'w').write(doc.toprettyxml())
        # World of horror! Python insists on not quoting quotes and
        # fixing the escaped &quot; into &amp;quot; whereas MSVS
        # requires quoted but not fixed elements. Enter horrible hack.
        txt = open(ofname, 'r').read()
        open(ofname, 'w').write(txt.replace('&amp;quot;', '&quot;'))

    def gen_testproj(self, target_name, ofname):
        buildtype = 'Debug'
        platform = "Win32"
        project_name = target_name
        root = ET.Element('Project', {'DefaultTargets' : "Build",
                                      'ToolsVersion' : '4.0',
                                      'xmlns' : 'http://schemas.microsoft.com/developer/msbuild/2003'})
        confitems = ET.SubElement(root, 'ItemGroup', {'Label' : 'ProjectConfigurations'})
        prjconf = ET.SubElement(confitems, 'ProjectConfiguration', {'Include' : 'Debug|Win32'})
        p = ET.SubElement(prjconf, 'Configuration')
        p.text= buildtype
        pl = ET.SubElement(prjconf, 'Platform')
        pl.text = platform
        globalgroup = ET.SubElement(root, 'PropertyGroup', Label='Globals')
        guidelem = ET.SubElement(globalgroup, 'ProjectGuid')
        guidelem.text = self.environment.coredata.test_guid
        kw = ET.SubElement(globalgroup, 'Keyword')
        kw.text = 'Win32Proj'
        p = ET.SubElement(globalgroup, 'Platform')
        p.text= platform
        pname= ET.SubElement(globalgroup, 'ProjectName')
        pname.text = project_name
        ET.SubElement(root, 'Import', Project='$(VCTargetsPath)\Microsoft.Cpp.Default.props')
        type_config = ET.SubElement(root, 'PropertyGroup', Label='Configuration')
        ET.SubElement(type_config, 'ConfigurationType')
        ET.SubElement(type_config, 'CharacterSet').text = 'MultiByte'
        ET.SubElement(type_config, 'UseOfMfc').text = 'false'
        ET.SubElement(root, 'Import', Project='$(VCTargetsPath)\Microsoft.Cpp.props')
        direlem = ET.SubElement(root, 'PropertyGroup')
        fver = ET.SubElement(direlem, '_ProjectFileVersion')
        fver.text = self.project_file_version
        outdir = ET.SubElement(direlem, 'OutDir')
        outdir.text = '.\\'
        intdir = ET.SubElement(direlem, 'IntDir')
        intdir.text = 'test-temp\\'
        tname = ET.SubElement(direlem, 'TargetName')
        tname.text = target_name

        action = ET.SubElement(root, 'ItemDefinitionGroup')
        midl = ET.SubElement(action, 'Midl')
        ET.SubElement(midl, "AdditionalIncludeDirectories").text = '%(AdditionalIncludeDirectories)'
        ET.SubElement(midl, "OutputDirectory").text = '$(IntDir)'
        ET.SubElement(midl, 'HeaderFileName').text = '%(Filename).h'
        ET.SubElement(midl, 'TypeLibraryName').text = '%(Filename).tlb'
        ET.SubElement(midl, 'InterfaceIdentifierFilename').text = '%(Filename)_i.c'
        ET.SubElement(midl, 'ProxyFileName').text = '%(Filename)_p.c'
        postbuild = ET.SubElement(action, 'PostBuildEvent')
        ET.SubElement(postbuild, 'Message')
        script_root = self.environment.get_script_dir()
        test_script = os.path.join(script_root, 'meson_test.py')
        test_data = os.path.join(self.environment.get_scratch_dir(), 'meson_test_setup.dat')
        cmd_templ = '''setlocal
"%s" "%s" "%s"
if %%errorlevel%% neq 0 goto :cmEnd
:cmEnd
endlocal & call :cmErrorLevel %%errorlevel%% & goto :cmDone
:cmErrorLevel
exit /b %%1
:cmDone
if %%errorlevel%% neq 0 goto :VCEnd'''
        ET.SubElement(postbuild, 'Command').text = cmd_templ % (sys.executable, test_script, test_data)
        ET.SubElement(root, 'Import', Project='$(VCTargetsPath)\Microsoft.Cpp.targets')
        tree = ET.ElementTree(root)
        tree.write(ofname, encoding='utf-8', xml_declaration=True)
        datafile = open(test_data, 'wb')
        self.write_test_file(datafile)
        datafile.close()
        # ElementTree can not do prettyprinting so do it manually
        #doc = xml.dom.minidom.parse(ofname)
        #open(ofname, 'w').write(doc.toprettyxml())
